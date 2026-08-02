# Metabase Dashboard

**Bridge, LAMF Pipeline**, eight cards, on the same Metabase instance Cadence
provisions. A second dashboard against the same database connection, not a
second Metabase deployment.

## Reproducible, not clicked together

Same discipline as Cadence's dashboard, for the same reason: a card built in the
Metabase UI lives only in Metabase's own application database, so it cannot be
reviewed, diffed, or restored, and it silently drifts from the Python analysis
it is meant to mirror.

- `sql/dashboard_questions.sql`: every card as a runnable query, each reading
  the same views (`v_lamf_pipeline`, `v_latest_portfolio`) that
  `loan_pipeline_report.py` reads, so the dashboard number and the automated
  report cannot quote two different disbursal rates.
- `scripts/provision_metabase.py`: parses that file and builds the cards and
  layout through the Metabase API. Idempotent: re-running updates existing
  cards rather than duplicating them.

## Bridge does not create its own database connection

This is the one thing that differs from Cadence's script, and it is deliberate.
Bridge extends Cadence's schema in the same Postgres instance, so a second
Metabase database connection to that instance would be a second, independently
syncing copy of the same schema cache, a source of exactly the kind of drift
this whole approach exists to prevent.

`scripts/provision_metabase.py` therefore looks up the connection Cadence's
own provisioning script already registered (named `"Cadence"` by default) and
builds Bridge's cards against it. If Cadence has not been provisioned yet, the
script fails immediately with that instruction rather than silently creating a
second connection.

## Setup

```bash
# 1. Cadence's database and Metabase must already be provisioned:
#    in the Cadence repo: make db-up && make schema && make run-sim
#      && make streak && make metabase-up && make dashboard

# 2. In this repo, load Bridge's data on top of Cadence's:
make schema && make run-sim

# 3. Provision Bridge's dashboard against the existing Metabase instance
#    (same METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD as Cadence's .env)
make dashboard
```

Then open `http://localhost:<METABASE_PORT>` (3001 by default, matching
Cadence's compose file). Both dashboards are visible from the same instance.

## The eight cards

| # | Card | Type | What decision it supports |
|---|---|---|---|
| 1 | LAMF funnel, last 12 weeks | bar | Applications to eligible to approved to disbursed, one source for every drop-off. |
| 2 | Disbursal rate vs the assumed band | line | Same [18%, 50%] band `loan_pipeline_report.py` alerts on, a visual trace of the same check. |
| 3 | Loan book composition by status | bar | Active / closed / defaulted, and the value in each. |
| 4 | Nudge outcome: borrowed vs withdrew | bar | A decision card: the raw counts the z-test in `nudge_validation.py` explains the significance of. |
| 5 | SIP breakage rate by arm | bar | What it cost users not to borrow, paired with card 4. |
| 6 | Average loan ticket vs the market-standard minimum | table | The decision card: is this book even viable to lend against. |
| 7 | Collateral coverage by threshold | bar | Same shape as `collateral_profile()`, live off the latest portfolio marks. |
| 8 | Net revenue at actuals | table | Quick read on the current loan book's fee revenue. |

Cards 6 and 7 are on the dashboard for the same reason card 8 is on Cadence's:
the constraint that governs whether this product line works at all should be
visible on the same screen as the pipeline metrics it constrains, not buried in
a separate memo nobody re-reads.

## What the dashboard currently shows

Verified against the live instance (14 tables, 5 views, shared with Cadence's
5,000 users and 373,387 transactions):

**Card 6, the number that reframes the whole project.** Average disbursed
ticket is Rs 1,218.99, against a market-standard minimum of Rs 25,000, or
4.9% of it. This is the same constraint `MIN_PORTFOLIO_FOR_LAMF` documents
in `generate_liquidity_events.py` and the breakeven-ticket calculation in
`unit_economics_calculator.py` derives independently (Rs 22,857, within 9% of
the industry figure). Three separate parts of this repo arrive at the same wall
by three different routes.

**Card 7, collateral coverage collapses to zero at the market standard.**
2,902 users clear Rs 500 of portfolio value; 22 clear Rs 10,000; zero clear
Rs 25,000. The curve does not taper gently, it goes to nothing exactly at the
threshold that matters.

**Card 5, the nudge works.** 45.2% control breakage vs 33.4% treatment, a
statistically significant -11.8pp among eligible users (p = 0.0029, full test
in `nudge_validation.py`). The one part of this project that is unambiguously
good news regardless of the collateral constraint.

**Card 3, the book is still small enough that "active" dominates.** 80 active
loans against 26 closed and, at the time of writing, zero observed defaults in
the live data, consistent with the assumed 1.2% default rate on a book this
size, though far too small a sample to confirm it.

## On screenshots

The dashboard is verified programmatically rather than screenshotted: every
card's query above was run directly against the live database, with the
actual row counts and values quoted next to it. That verification rebuilds
on any machine rather than describing one instant of one instance. Run
`make dashboard` and look at the real thing.

Two of the underlying charts are committed as static images and shown in the
main [README](../README.md#core-analysis) and the
[live findings page](https://navneet-scaler.github.io/bridge/): the
propensity model's odds-ratio plot and the nudge validation's breakage
comparison, both produced by the analysis code in this repository, not
mocked up separately.
