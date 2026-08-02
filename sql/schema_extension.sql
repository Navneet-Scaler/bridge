-- =============================================================================
-- Bridge — LAMF cross-sell schema, extending Cadence's
-- =============================================================================
--
-- This script is an EXTENSION, not a standalone schema. It assumes Cadence's
-- tables already exist in this database (users, sip_daily_transactions,
-- user_streaks, v_user_consistency) and fails loudly if they do not — see the
-- dependency guard immediately below. A missing dependency should stop the run,
-- not silently produce an unlinked island of tables.
--
-- Layered the same way Cadence's is, because the layers mean different things:
--
--   1. PRODUCTION-SHAPED tables (portfolio_snapshots, withdrawal_events,
--      loan_applications, loans, borrow_nudges) — what a real LAMF product
--      would persist.
--
--   2. GOVERNANCE (data_quality_flags) — already created by Cadence. Bridge
--      writes its loan-side findings into the same table rather than starting a
--      second one, so a reviewer sees one quality log for the whole system.
--
--   3. SIMULATION GROUND TRUTH (sim_liquidity_events) — the counterfactual
--      branch each synthetic liquidity event was generated with. No production
--      equivalent exists. Kept in its own table precisely so it can never be
--      mistaken for an observed field.
--
-- A NOTE ON THE NUMBERS IN THIS FILE
-- ----------------------------------
-- The only figures hard-coded here are the two the product itself advertises:
-- a 9.99% p.a. interest rate and an 80% maximum loan-to-value. They are DEFAULTS
-- for simulated rows, not business assumptions about BlinkMoney's economics.
-- Every revenue-side number — take rate, default rate, disbursal rate — lives in
-- src/economics/unit_economics_calculator.py as a named constant and is
-- documented in ASSUMPTIONS.md. None of it belongs in the schema.
--
-- Idempotent: safe to re-run. Drops Bridge's own objects in FK-dependency order
-- and never touches Cadence's.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 0. DEPENDENCY GUARD
-- -----------------------------------------------------------------------------
-- Bridge's foreign keys point at Cadence's users table. Running this against an
-- empty database would otherwise fail with a confusing "relation does not exist"
-- halfway through; this turns it into one clear sentence naming the fix.
DO $$
BEGIN
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION
            'Bridge extends Cadence''s schema, but table "users" was not found. '
            'Apply Cadence''s sql/schema.sql to this database first.';
    END IF;
    IF to_regclass('public.user_streaks') IS NULL THEN
        RAISE EXCEPTION
            'Table "user_streaks" was not found. Bridge''s propensity model reads '
            'investing consistency from Cadence''s streak output — run Cadence''s '
            'streak builder before applying this extension.';
    END IF;
END
$$;


DROP VIEW  IF EXISTS v_lamf_pipeline        CASCADE;
DROP VIEW  IF EXISTS v_latest_portfolio     CASCADE;
DROP TABLE IF EXISTS sim_liquidity_events   CASCADE;
DROP TABLE IF EXISTS borrow_nudges          CASCADE;
DROP TABLE IF EXISTS loans                  CASCADE;
DROP TABLE IF EXISTS loan_applications      CASCADE;
DROP TABLE IF EXISTS withdrawal_events      CASCADE;
DROP TABLE IF EXISTS portfolio_snapshots    CASCADE;


-- =============================================================================
-- 1. PRODUCTION-SHAPED TABLES
-- =============================================================================

-- Daily mark-to-market value of a user's holdings. This is the collateral base:
-- every eligibility decision and every loan-to-value ratio is computed from it.
CREATE TABLE portfolio_snapshots (
    snapshot_id     SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(user_id),
    snapshot_date   DATE NOT NULL,
    portfolio_value NUMERIC(12,2) NOT NULL CHECK (portfolio_value >= 0),
    UNIQUE (user_id, snapshot_date)
);

COMMENT ON TABLE portfolio_snapshots IS
    'Daily mark-to-market portfolio value per user. Grown from Cadence''s successful sip_daily_transactions compounding at the assumed market return — so a user''s investing consistency drives their collateral base directly, which is the mechanism linking the two projects.';
COMMENT ON COLUMN portfolio_snapshots.portfolio_value IS
    'Collateral base for the loan-to-value calculation. UNIQUE on (user_id, snapshot_date) because two marks for one user-day is a data error, not a legitimate history — the constraint Cadence''s transaction table deliberately lacks (DQ-02), applied here from the start.';

-- A user taking money out. The `sip_broken` flag is the outcome the whole
-- project exists to prevent.
CREATE TABLE withdrawal_events (
    withdrawal_id   SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(user_id),
    event_date      DATE NOT NULL,
    amount          NUMERIC(12,2) CHECK (amount > 0),
    sip_broken      BOOLEAN NOT NULL
);

COMMENT ON TABLE withdrawal_events IS
    'Money leaving the platform. One row per withdrawal.';
COMMENT ON COLUMN withdrawal_events.sip_broken IS
    'TRUE when this withdrawal stopped the daily SIP. This is the outcome variable of the whole project: a withdrawal that leaves the SIP running costs AUM once, a withdrawal that breaks it costs the compounding stream as well.';

-- A request to borrow. Kept separate from `loans` because the funnel drop-offs
-- between applied, eligible, approved and disbursed are exactly what the LAMF
-- pipeline dashboard has to expose.
CREATE TABLE loan_applications (
    application_id   SERIAL PRIMARY KEY,
    user_id          INT NOT NULL REFERENCES users(user_id),
    applied_date     DATE NOT NULL,
    requested_amount NUMERIC(12,2) CHECK (requested_amount > 0),
    eligible         BOOLEAN,
    approved         BOOLEAN
);

COMMENT ON TABLE loan_applications IS
    'One row per borrow request. Separate from loans so the funnel — applied -> eligible -> approved -> disbursed — stays visible; collapsing them into one table would hide exactly the drop-offs the pipeline dashboard exists to surface.';
COMMENT ON COLUMN loan_applications.eligible IS
    'Rule-based screen (KYC verified, portfolio above the minimum, tenure above the minimum). NULL means not yet evaluated, which is different from FALSE.';

-- A disbursed loan. Only applications that were approved get one.
CREATE TABLE loans (
    loan_id          SERIAL PRIMARY KEY,
    application_id   INT NOT NULL UNIQUE REFERENCES loan_applications(application_id),
    disbursed_amount NUMERIC(12,2) NOT NULL CHECK (disbursed_amount > 0),
    disbursal_date   DATE NOT NULL,
    interest_rate    NUMERIC(5,2) NOT NULL DEFAULT 9.99,
    tenure_months    INT NOT NULL CHECK (tenure_months > 0),
    status           VARCHAR(20) NOT NULL
                     CHECK (status IN ('active', 'closed', 'default'))
);

COMMENT ON TABLE loans IS
    'Disbursed loans. UNIQUE on application_id: one application can produce at most one loan, and without the constraint a double-disbursal bug would silently double the loan book.';
COMMENT ON COLUMN loans.interest_rate IS
    'Annual rate charged to the borrower, defaulting to the 9.99% the product advertises. This is a PRODUCT figure, not an assumption about BlinkMoney''s margin — what the company earns on it is modelled separately and documented in ASSUMPTIONS.md.';
COMMENT ON COLUMN loans.status IS
    'active | closed | default. Constrained here on purpose: Cadence''s equivalent status column has no CHECK (its DQ-04), and the resulting case-mismatched rows silently broke streak construction. Not repeating that.';

-- A "borrow instead of withdraw" prompt. The intervention under test.
CREATE TABLE borrow_nudges (
    nudge_id         SERIAL PRIMARY KEY,
    user_id          INT NOT NULL REFERENCES users(user_id),
    triggered_by     VARCHAR(50) NOT NULL
                     CHECK (triggered_by IN ('withdrawal_attempt', 'liquidity_signal')),
    sent_date        DATE NOT NULL,
    resulted_in_loan BOOLEAN NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE borrow_nudges IS
    'The intervention: a prompt to borrow against the portfolio rather than sell it. Fired at the moment a liquidity need is detected.';
COMMENT ON COLUMN borrow_nudges.resulted_in_loan IS
    'Outcome of the nudge. STRICTLY off-limits as a propensity model feature — it is only known after the event being predicted, and using it would be textbook leakage. tests/test_feature_engineering.py asserts it never enters the feature set.';


-- =============================================================================
-- 2. SIMULATION GROUND TRUTH (no production equivalent)
-- =============================================================================

-- Each simulated liquidity need, with the branch it was assigned to. This is
-- what makes a treatment-vs-control comparison possible at all: the same event
-- is generated once, then resolved differently depending on the arm.
CREATE TABLE sim_liquidity_events (
    event_id        SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(user_id),
    event_date      DATE NOT NULL,
    needed_amount   NUMERIC(12,2) NOT NULL CHECK (needed_amount > 0),
    arm             VARCHAR(20) NOT NULL CHECK (arm IN ('treatment', 'control')),
    was_eligible    BOOLEAN NOT NULL,
    resolution      VARCHAR(20) NOT NULL
                    CHECK (resolution IN ('withdrew', 'borrowed'))
);

COMMENT ON TABLE sim_liquidity_events IS
    'SIMULATION ONLY — the generated cash-need events and how each resolved. Never treat as an observed field. Kept in its own table so it cannot be mistaken for production data, and so the analysis can be checked against the effect that was planted.';
COMMENT ON COLUMN sim_liquidity_events.arm IS
    'treatment = shown the borrow nudge, control = not shown. Assigned at the event, independently of the user''s attributes, so the comparison is not contaminated by selection on who looked like a good borrower.';
COMMENT ON COLUMN sim_liquidity_events.was_eligible IS
    'Whether the user cleared the eligibility screen at event time. Recorded separately from resolution because an ineligible treatment user cannot borrow however good the nudge is — mixing the two would understate the nudge''s effect on the population it can actually reach.';


-- =============================================================================
-- 3. VIEWS — the reading layer
-- =============================================================================

-- Most recent mark per user. Written once here so no downstream query has to
-- re-derive "latest" with its own window function and get it subtly different.
CREATE VIEW v_latest_portfolio AS
SELECT DISTINCT ON (user_id)
       user_id,
       snapshot_date AS as_of_date,
       portfolio_value
FROM portfolio_snapshots
ORDER BY user_id, snapshot_date DESC;

COMMENT ON VIEW v_latest_portfolio IS
    'Latest portfolio mark per user. DISTINCT ON is the cheap Postgres idiom for a per-group top-1; defining it once stops five dashboards each inventing their own "latest".';

-- The LAMF funnel, one row per stage, which is what the pipeline dashboard and
-- the weekly automated report both read.
CREATE VIEW v_lamf_pipeline AS
WITH stages AS (
    SELECT a.application_id,
           a.user_id,
           a.applied_date,
           a.requested_amount,
           COALESCE(a.eligible, FALSE) AS eligible,
           COALESCE(a.approved, FALSE) AS approved,
           l.loan_id IS NOT NULL       AS disbursed,
           l.disbursed_amount,
           l.status                    AS loan_status
    FROM loan_applications a
    LEFT JOIN loans l ON l.application_id = a.application_id
)
SELECT date_trunc('week', applied_date)::date       AS week_start,
       COUNT(*)                                     AS applications,
       COUNT(*) FILTER (WHERE eligible)             AS eligible,
       COUNT(*) FILTER (WHERE approved)             AS approved,
       COUNT(*) FILTER (WHERE disbursed)            AS disbursed,
       COALESCE(SUM(disbursed_amount), 0)           AS disbursed_value,
       COUNT(*) FILTER (WHERE loan_status = 'default') AS defaults
FROM stages
GROUP BY 1;

COMMENT ON VIEW v_lamf_pipeline IS
    'Weekly LAMF funnel. Counts every stage from the same base so the drop-offs reconcile by construction — the disbursal rate on the dashboard and the one in the weekly report cannot disagree, because there is only one definition.';


-- =============================================================================
-- 4. INDEXES
-- =============================================================================
-- Every Bridge index is prefixed `idx_bridge_`.
--
-- This is not decoration. Index names in Postgres are global to the schema, not
-- scoped to their table, and Bridge shares a schema with Cadence. The first
-- version of this file used `idx_nudges_user` for borrow_nudges and collided
-- head-on with Cadence's identically-named index on nudges_sent — the two tables
-- have different names but the obvious index name for both is the same. A prefix
-- makes the whole namespace collision-proof rather than fixing one name and
-- waiting for the next clash.
--
-- Portfolio lookups are always "this user, at or before this date", which is
-- exactly what the leading composite index serves.
CREATE INDEX idx_bridge_portfolio_user_date ON portfolio_snapshots (user_id, snapshot_date DESC);
CREATE INDEX idx_bridge_withdrawal_user     ON withdrawal_events (user_id, event_date);
CREATE INDEX idx_bridge_withdrawal_broken   ON withdrawal_events (sip_broken);
CREATE INDEX idx_bridge_applications_user   ON loan_applications (user_id, applied_date);
CREATE INDEX idx_bridge_applications_date   ON loan_applications (applied_date);
CREATE INDEX idx_bridge_loans_status        ON loans (status);
CREATE INDEX idx_bridge_loans_disbursal     ON loans (disbursal_date);
CREATE INDEX idx_bridge_nudges_user         ON borrow_nudges (user_id, sent_date);
CREATE INDEX idx_bridge_sim_events_user     ON sim_liquidity_events (user_id, event_date);
CREATE INDEX idx_bridge_sim_events_arm      ON sim_liquidity_events (arm, was_eligible);
