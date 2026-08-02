# Bridge

**LAMF cross-sell & unit economics engine** — the decision layer for *borrow instead of break*.

The build is complete. Read [MEMO.md](MEMO.md) for the one-page answer to "if we
push LAMF harder, what happens" and [ASSUMPTIONS.md](ASSUMPTIONS.md) before
repeating any number from this repo elsewhere.

## What this is

BlinkMoney's pitch to the user is simple: don't break your SIP to raise cash, borrow
against it instead — up to 80% of portfolio value at 9.99% p.a., while the portfolio
keeps compounding. Every user who withdraws instead of borrowing is AUM the company
loses. Every user who borrows instead is AUM retained *plus* a lending relationship.

Bridge scores users on LAMF candidacy, models what happens to revenue and risk if
disbursal is scaled, validates whether nudging "borrow instead of withdraw" actually
works, and reports on the pipeline automatically.

**Headline finding:** this cohort's portfolios are too small to lend against at
standard LAMF economics — a constraint three independent parts of this pipeline
converge on separately. The nudge to borrow instead of withdraw works regardless,
and consistency beats portfolio size 5-to-1 as a predictor of who to target once
the collateral problem is solved. Full story in `MEMO.md`.

## Relationship to Cadence

Bridge is the second module in the same suite as
[Cadence](https://github.com/Navneet-Scaler/cadence) and shares its data layer —
it is **not** a standalone project with its own database.

- Bridge's schema *extends* Cadence's: its tables carry foreign keys into Cadence's
  `users` table, and both read the same `sip_daily_transactions`.
- Cadence computes each user's streak length and investing consistency. Bridge feeds
  that straight into its propensity model as a feature, on the premise that someone
  who invests reliably every day is a structurally different borrower from someone
  who invests erratically. It's the single strongest predictor in the model — 5x
  more than portfolio size.
- `docker-compose.yml` here pins the Compose project name to `cadence` so bringing
  it up attaches to the *same* container and volume rather than creating a second one.
- The Metabase dashboard reuses Cadence's existing database connection rather than
  registering a second one against the same Postgres instance.

**Build order matters:** Cadence must exist and have populated `users`,
`sip_daily_transactions`, and `user_streaks` before Bridge's feature engineering can
run.

## Running it

```bash
# 1. Cadence must already be running with data loaded (in the Cadence repo):
#    make db-up && make schema && make run-sim && make streak

# 2. In this repo:
make setup        # venv, deps, pre-commit
make schema       # apply Bridge's schema extension
make run-sim      # portfolio growth + liquidity events + treatment/control
make features     # build the propensity feature set
make train-model  # fit and evaluate the propensity model
make economics    # unit economics + scenario table
make nudge        # treatment/control significance test
make report       # weekly pipeline health report
make dashboard    # provision the Metabase dashboard

# or the whole pipeline at once:
make all
```

`make test` runs the full suite (112 tests). `make lint` / `make fmt` for
formatting and linting.

## Repository layout

```
sql/schema_extension.sql          — the five LAMF tables + views, extends Cadence
sql/dashboard_questions.sql       — every Metabase card as a runnable query
src/simulate/                     — portfolio growth, liquidity events, treatment/control
src/modeling/                     — leakage-safe features + the propensity model
src/economics/                    — the unit economics calculator
src/analysis/                     — nudge validation (z-tests) + shared chart styling
src/reporting/                    — the weekly automated pipeline report
scripts/provision_metabase.py     — builds the dashboard from sql/dashboard_questions.sql
tests/                            — 112 tests, one file per module above
ASSUMPTIONS.md                    — every business number, tagged PRODUCT/INDUSTRY/ASSUMED
MEMO.md                           — the one-page decision memo
CHANGELOG.md                      — what shipped, phase by phase
dashboards/metabase_notes.md      — dashboard setup + what it currently shows
```

## What each phase found

| Phase | What it built | What it found |
|---|---|---|
| Schema | LAMF tables extending Cadence's shared DB | — |
| Simulation | Portfolio growth from real transaction history + liquidity events | Median portfolio Rs 862 — far below any realistic LAMF minimum |
| Features | Leakage-safe feature set, split at a temporal cutoff | An off-by-one letting a ratio feature exceed 1.0 |
| Propensity model | Logistic regression, AUC 0.870 | Consistency predicts candidacy 5x better than portfolio size |
| Economics | Provenance-tagged unit economics calculator | Breakeven ticket Rs 22,857 — independently within 9% of the real industry minimum |
| Nudge validation | Two-proportion z-tests, balance-checked | Nudge cuts SIP breakage 11.8pp (p=0.0029) |
| Automation | Weekly report, actuals vs. assumed bands | A `date.today()`-anchored window silently missing all historical data |
| Dashboard | 8 cards, provisioned from committed SQL | Collateral coverage hits exactly zero at the real minimum ticket |

## A note on the numbers

BlinkMoney's real take rate, margin structure, and default experience are not public.
Every business input in this repo is an **explicitly labelled assumption**, declared
as a named constant tagged `PRODUCT`, `INDUSTRY`, or `ASSUMED` and documented in
[ASSUMPTIONS.md](ASSUMPTIONS.md) — never presented as researched fact. The most
consequential of these — a Rs 2,000 simulated eligibility threshold, ~12x under the
real market minimum — is the reason every revenue figure in this repository should
be read as an upper bound, not a projection.
