# Bridge

**LAMF cross-sell & unit economics engine** — the decision layer for *borrow instead of break*.

Work in progress. See [ASSUMPTIONS.md](ASSUMPTIONS.md) and [MEMO.md](MEMO.md) once published.

## What this is

BlinkMoney's pitch to the user is simple: don't break your SIP to raise cash, borrow
against it instead — up to 80% of portfolio value at 9.99% p.a., while the portfolio
keeps compounding. Every user who withdraws instead of borrowing is AUM the company
loses. Every user who borrows instead is AUM retained *plus* a lending relationship.

Bridge scores users on LAMF candidacy, models what happens to revenue and risk if
disbursal is scaled, and fires a "borrow instead of withdraw" nudge when a liquidity
signal appears.

## Relationship to Cadence

Bridge is the second module in the same suite as
[Cadence](https://github.com/Navneet-Scaler/cadence) and shares its data layer —
it is **not** a standalone project with its own database.

- Bridge's schema *extends* Cadence's: its tables carry foreign keys into Cadence's
  `users` table, and both read the same `sip_daily_transactions`.
- Cadence computes each user's streak length and investing consistency. Bridge feeds
  that straight into its propensity model as a feature, on the premise that someone
  who invests reliably every day is a structurally different borrower from someone
  who invests erratically.
- `docker-compose.yml` here pins the Compose project name to `cadence` so bringing
  it up attaches to the *same* container and volume rather than creating a second one.

**Build order matters:** Cadence must exist and have populated `users`,
`sip_daily_transactions`, and `user_streaks` before Bridge's feature engineering can
run.

## Status

| Phase | State |
|---|---|
| Repo scaffold, tooling, CI | done |
| Schema extension | in progress |
| Liquidity + treatment/control simulation | pending |
| Propensity model | pending |
| Unit economics calculator | pending |
| Nudge validation | pending |
| Automation & dashboard | pending |

## A note on the numbers

BlinkMoney's real take rate, margin structure, and default experience are not public.
Every business input in this repo is therefore an **explicitly labelled assumption**,
declared as a named constant and documented in `ASSUMPTIONS.md` — never presented as
researched fact.
