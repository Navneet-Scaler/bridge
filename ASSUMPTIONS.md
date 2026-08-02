# Assumptions

Every business number used anywhere in Bridge, in one place. This file exists
because the difference between "I found this" and "I'm modeling this" is the
entire credibility of the project, and a confident-looking wrong number is worse
than no number at all.

Three provenance tags are used throughout the codebase (see
`src/economics/unit_economics_calculator.py::Provenance`) and repeated here:

| tag | meaning |
|---|---|
| **PRODUCT** | advertised by the product itself — verifiable, not modeled |
| **INDUSTRY** | a structural feature of how LAMF lending works, reasoned from the product's mechanics — not measured from any published portfolio |
| **ASSUMED** | chosen for the purpose of building this model — no source, argue with it |

Anything tagged `ASSUMED` is a guess made so the analysis could proceed. Treat
every number derived from it as conditional on that guess, not as a finding.

---

## Product facts (PRODUCT)

| constant | value | where |
|---|---|---|
| Borrower interest rate | **9.99% p.a.** | `borrower_rate`, advertised by BlinkMoney |
| Maximum loan-to-value | **80%** | `max_ltv`, advertised by BlinkMoney |

These are the only two numbers in the entire repository not tagged `ASSUMED` or
`INDUSTRY` — verifiable facts about the product as pitched, not modeling
choices.

## Structural / industry reasoning (INDUSTRY)

| constant | value | reasoning |
|---|---|---|
| Default rate | **1.2%** | LAMF is over-collateralised by liquid mutual fund units, redeemable on a margin call. Structurally far below unsecured consumer credit default rates. Reasoned from that mechanism, **not measured from any published BlinkMoney or industry portfolio**. |
| Loss given default | **25%** | The collateral is liquid and marked daily, so the loss on a defaulted loan is the gap between the margin-call trigger and the realised sale price, not the full balance. |
| Market-standard minimum ticket | **Rs 25,000** | Typical minimum for LAMF products in India, because origination and servicing cost is roughly fixed per loan and a small loan cannot carry it. This number is the single most consequential assumption in the repository — see below. |

## Modeling assumptions (ASSUMED — no source)

| constant | value | where used | why this number |
|---|---|---|---|
| Assumed market return | **15% p.a.** | Portfolio growth simulation, user-value calculation | The return the product's own "keep compounding" pitch implies. A forward return is not knowable; this is a planning input, not a forecast. |
| Annualised volatility | **14%** | Portfolio growth simulation | Without volatility, collateral never falls and loan-to-value risk is structurally impossible to model — the whole risk section would be begging its own question. |
| Origination fee rate | **1.5%** | `origination_fee_rate` — **the single largest driver of modeled revenue** | BlinkMoney's actual commercial terms in a Lending Service Provider arrangement are not public. This is a placeholder chosen to be in a plausible range for a sourcing fee, nothing more. Swept in every scenario table because the answer is roughly linear in it. |
| Servicing cost per loan | **Rs 180/year** | Unit economics | Collections, support, reconciliation. Modeled as **fixed per loan regardless of size** — this is the assumption that makes small tickets uneconomic in the model, and it is a genuine industry pattern (fixed servicing cost is why minimum tickets exist at all), but the specific number is a guess. |
| Acquisition cost per disbursal | **Rs 60** | Unit economics | Low because this is cross-sell to an already-acquired user, not new-user acquisition. No source. |
| Risk provision multiple | **1.5x** expected loss | Unit economics | Expected loss is an average; a correlated market drawdown would trigger margin calls across the whole book simultaneously, since the risk is not independent across users. The multiple is a prudence buffer, chosen rather than derived. |
| Eligibility: minimum portfolio | **Rs 2,000** | Simulation eligibility screen | **Not the real minimum** — see the dedicated section below. Set ~12x under the market standard purely so the simulation has an addressable population to analyze. |
| Eligibility: minimum tenure | **90 days** | Simulation eligibility screen | A brand-new account is not a lending relationship. Round number, not derived. |
| Nudge uptake rate | **34%** | Simulation (of eligible users shown the nudge) | No public figure exists for BlinkMoney's LAMF conversion rate. This is a simulation *input* — the fact that the trained pipeline recovers ~34.4% in the resolved data is a check that the simulation code works, **not evidence about how real users would behave**. |
| SIP breakage without nudge | **45%** base rate | Simulation | A withdrawal doesn't always kill a SIP; larger withdrawals are less survivable. Modeled as a floor plus a slope on withdrawal size, both chosen rather than fit to any data. |
| SIP breakage with nudge | **33%** base rate | Simulation | Same caveat. The *difference* between these two, not either number alone, is what phase 6 actually tests statistically once real event data is generated. |

---

## The constraint that reframes the whole project

This is the most important paragraph in this file.

Loan-against-mutual-funds products in India typically require a **~Rs 25,000
minimum ticket** (INDUSTRY, above), because origination and servicing costs are
roughly fixed per loan. Applying that minimum to this cohort — a one-year-old
Rs 21/day micro-SIP book — makes **every single user ineligible**:

- Median portfolio value at the end of the simulated year: **Rs 862**
- Maximum portfolio value across all 5,000 users: **Rs 11,841**
- Users clearing Rs 25,000: **zero**

This was discovered, not assumed: the first version of the simulation used the
real Rs 25,000 threshold and generated zero liquidity events, because no user
qualified. `MIN_PORTFOLIO_FOR_LAMF` was then set to **Rs 2,000** — an `ASSUMED`
value chosen purely so phases 3 through 6 would have a population to analyze,
documented as such directly above the constant in
`src/simulate/generate_liquidity_events.py`.

**Consequence: every revenue figure in this repository is an upper bound on a
loan book that could not actually be written today at standard market
economics.** The unit economics model's own breakeven-ticket calculation
(`breakeven_ticket()`) derives **Rs 22,857** independently from the fixed-cost
assumptions above — within 9% of the industry figure, arrived at by a completely
different route (cost structure rather than market convention). Three separate
parts of this repository converge on the same wall:

1. The simulation cannot seed eligible users at the market minimum.
2. The economics model's own cost structure implies almost the same minimum.
3. The live database dashboard shows collateral coverage falling to exactly
   zero at Rs 25,000, not tapering toward it.

`MEMO.md` leads with this, not with a disbursal-rate projection, because pushing
disbursal rate on a book this thin makes losses worse, not better — a result
`portfolio_maturity_sweep()` shows directly (net revenue per loan is *more*
negative in the optimistic scenario than the pessimistic one, since more loans
multiply a fixed per-loan loss).

---

## What this file is not

Not a claim that any number above is wrong, and not a claim that the real
figures are close to these. It is a record of exactly what was guessed, so that
guess can be replaced the moment a real number is available — a `PRODUCT` or
`INDUSTRY` figure with a citation, or an `ASSUMED` figure a founder overrides
with their own judgment.
