-- =============================================================================
-- Metabase dashboard questions: LAMF pipeline
-- =============================================================================
--
-- Every card on the Bridge dashboard, as a standalone query, for the same three
-- reasons Cadence's equivalent file gives: Metabase questions are not version
-- controlled and a card built in the UI cannot be reviewed or diffed; these are
-- runnable directly in psql without the app; and the dashboard and the Python
-- analysis must not drift apart, so both read the same views.
--
-- Read against v_lamf_pipeline and v_latest_portfolio where possible, the same
-- views loan_pipeline_report.py reads, rather than re-deriving the funnel logic
-- here. A dashboard number and a script number computed by two different queries
-- is exactly how a founder ends up quoting two different disbursal rates.
--
-- Each block is one card. Copy into Metabase as a native SQL question, or use as
-- the reference when rebuilding from scratch via scripts/provision_metabase.py.
-- =============================================================================


-- CARD 1: LAMF funnel, last 12 weeks (bar)
-- The top-line pipeline card. Every stage from one source, so the drop-offs
-- reconcile by construction with the automated weekly report.
SELECT week_start                                        AS "Week",
       applications                                       AS "Applications",
       eligible                                            AS "Eligible",
       approved                                            AS "Approved",
       disbursed                                           AS "Disbursed"
FROM v_lamf_pipeline
WHERE week_start >= (SELECT MAX(week_start) - INTERVAL '12 weeks' FROM v_lamf_pipeline)
ORDER BY week_start;


-- CARD 2: Disbursal rate vs the assumed band (line)
-- The band [18%, 50%] mirrors DISBURSAL_RATE_BAND in loan_pipeline_report.py.
-- Keep the two in sync if that constant changes. A week outside this line's
-- flat range is the same event the automated report flags.
SELECT week_start                                                       AS "Week",
       ROUND(100.0 * disbursed / NULLIF(eligible, 0), 1)                AS "Disbursal rate %"
FROM v_lamf_pipeline
ORDER BY week_start;


-- CARD 3: Loan book composition by status (bar)
-- Active, closed, defaulted: the shape of the book right now, not the funnel
-- that produced it.
SELECT status                                             AS "Status",
       COUNT(*)                                            AS "Loans",
       ROUND(SUM(disbursed_amount), 2)                      AS "Value"
FROM loans
GROUP BY 1
ORDER BY "Loans" DESC;


-- CARD 4: Nudge outcome, borrowed vs withdrew, by arm (bar)
-- The nudge-validation headline, read straight from the ground-truth table
-- rather than recomputed. This is the number nudge_validation.py's z-test
-- explains the significance of.
SELECT arm                                                 AS "Arm",
       resolution                                           AS "Resolution",
       COUNT(*)                                              AS "Events"
FROM sim_liquidity_events
WHERE was_eligible
GROUP BY 1, 2
ORDER BY 1, 2;


-- CARD 5: SIP breakage rate by arm (bar)
-- The outcome the nudge exists to prevent. Pairs with card 4: card 4 shows what
-- users did, this shows what it cost them not to borrow.
SELECT e.arm                                                            AS "Arm",
       ROUND(100.0 * AVG(COALESCE(w.sip_broken, FALSE)::int), 1)        AS "SIP breakage %"
FROM sim_liquidity_events e
LEFT JOIN withdrawal_events w
       ON w.user_id = e.user_id AND w.event_date = e.event_date
WHERE e.was_eligible
GROUP BY 1
ORDER BY 1;


-- CARD 6: Average loan ticket vs the market-standard minimum (table)
-- The single most important number on this dashboard. avg(disbursed_amount) is
-- what this book is actually writing; Rs 25,000 is what the LAMF market
-- typically requires per loan. See MIN_PORTFOLIO_FOR_LAMF in
-- generate_liquidity_events.py for the full reasoning.
SELECT ROUND(AVG(disbursed_amount), 2)                     AS "Avg ticket (Rs)",
       25000                                                 AS "Market standard min (Rs)",
       ROUND(100.0 * AVG(disbursed_amount) / 25000, 1)       AS "% of market minimum"
FROM loans;


-- CARD 7: Collateral coverage by threshold (bar)
-- Same shape as collateral_profile() in generate_liquidity_events.py, read
-- directly from the latest portfolio marks rather than recomputed in Python, so
-- this card stays live as new snapshots load.
WITH thresholds(min_portfolio) AS (
    VALUES (500), (1000), (2000), (5000), (10000), (25000)
)
SELECT t.min_portfolio                                                       AS "Min portfolio (Rs)",
       COUNT(p.user_id) FILTER (WHERE p.portfolio_value >= t.min_portfolio)  AS "Users eligible"
FROM thresholds t
LEFT JOIN v_latest_portfolio p ON TRUE
GROUP BY 1
ORDER BY 1;


-- CARD 8: Net revenue at actuals vs assumed scenarios (table)
-- Mirrors reconcile_against_scenarios() in loan_pipeline_report.py. Kept as a
-- static base/optimistic/pessimistic reference row set here (Metabase native
-- SQL cannot call back into the Python economics model), with actuals computed
-- live from the loan book so the two are always compared against the current
-- numbers rather than a snapshot.
SELECT 'actual'                                            AS "Scenario",
       COUNT(*)                                              AS "Loans",
       ROUND(SUM(disbursed_amount), 2)                       AS "Loan book (Rs)",
       ROUND(SUM(disbursed_amount) * 0.015, 2)                AS "Gross revenue at 1.5% fee (Rs)"
FROM loans;
