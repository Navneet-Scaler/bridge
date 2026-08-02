# Changelog

All notable changes to Bridge are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] — Initial build complete

The full LAMF cross-sell and unit economics pipeline, end to end: schema
extension, simulation, propensity model, economics calculator, nudge
validation, automated reporting, and dashboard.

### Added

- **Schema** — five tables (`portfolio_snapshots`, `withdrawal_events`,
  `loan_applications`, `loans`, `borrow_nudges`) plus `sim_liquidity_events`,
  extending Cadence's shared database with foreign keys into its `users`
  table. A dependency guard fails loudly if Cadence's schema is missing rather
  than partway through a cryptic error.
- **Liquidity simulation** — portfolios compounded from Cadence's real
  transaction history via geometric Brownian motion with a shared market
  shock; liquidity events with planted city-tier and tenure structure;
  treatment/control resolution assigned independently of user attributes.
  Surfaced that this cohort's median portfolio (Rs 862) sits far below any
  realistic LAMF eligibility threshold.
- **Feature engineering** — a leakage-safe feature set split at a temporal
  cutoff, pulling investing consistency directly from Cadence's
  `user_streaks`. Fixed an off-by-one that let `active_day_ratio` exceed 1.0.
- **Propensity model** — logistic regression chosen for explainability
  (ROC-AUC 0.870, AP 0.369 at an 11% base rate). Caught and fixed severe
  multicollinearity (VIF 72.8) between `successful_days` and two other
  features that was producing contradictory coefficient signs.
- **Unit economics calculator** — every business input tagged `PRODUCT`,
  `INDUSTRY`, or `ASSUMED`. Derived a Rs 22,857 breakeven ticket independently
  of the simulation, landing within 9% of the industry's real minimum.
- **Nudge validation** — two-proportion z-tests with Newcombe confidence
  intervals, a pre-outcome randomisation balance check, and an
  intention-to-treat estimate alongside the eligible-only one. Found the
  nudge cuts SIP breakage by 11.8 percentage points among eligible users
  (p = 0.0029).
- **Automation** — `loan_pipeline_report.py`, recomputing economics from
  actual weekly pipeline data and flagging weeks outside the expected
  disbursal/default bands. Fixed a real bug where a `date.today()`-anchored
  window silently found nothing against historical simulated data.
- **Dashboard** — eight Metabase cards provisioned from
  `sql/dashboard_questions.sql`, sharing Cadence's existing database
  connection rather than creating a duplicate.
- **Docs** — `ASSUMPTIONS.md`, `MEMO.md`, this changelog, and a README tying
  the whole pipeline together.

### Found

- This cohort cannot support LAMF at the market-standard ~Rs 25,000 minimum
  ticket — zero users clear it. Every revenue figure in this repository is an
  upper bound on a book that can't be written today at standard economics.
  Three independent parts of the pipeline (the simulation's eligible
  population, the calculator's cost-derived breakeven ticket, and the live
  collateral-coverage query) converge on the same constraint.
- Investing consistency (`active_day_ratio`) is **5x more predictive** of LAMF
  candidacy than portfolio size — the direct payoff of feeding Cadence's
  streak output into this model instead of scoring on AUM alone.
- The borrow-instead-of-withdraw nudge works and is independent of the
  collateral problem: an 11.8pp reduction in SIP breakage among users who can
  actually act on it.

### Notes

Every commit on `dev` is a single merged PR per phase (schema → simulation →
features → model → economics → nudge → automation → dashboard → docs), each
with CI passing (`black`, `ruff`, `pytest`) before merge. 112 tests across the
suite. No trained model binaries, no generated data, and no real credentials
are committed at any point in this history.
