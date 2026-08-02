# Memo: LAMF cross-sell, where it stands

**If we push LAMF harder, who should we target, and what does it do to revenue?**

Short answer: not yet, and pushing disbursal rate makes it worse, not
better. The binding constraint isn't targeting or conversion. It's that this
cohort's portfolios aren't big enough to lend against at standard economics.
Once that's fixed, targeting is straightforward: investing consistency beats
portfolio size 5-to-1 as a predictor of who to approach.

## The constraint, first

LAMF products in India typically need a ~Rs 25,000 minimum ticket to cover
fixed origination and servicing costs. This book, one year into a Rs 21/day
micro-SIP, has a median portfolio of Rs 862 and a maximum of Rs 11,841.
Nobody clears the real minimum. We only got simulated loans at all by
assuming a Rs 2,000 threshold, roughly 12x under market, purely so the rest
of this analysis had a population to work with (`ASSUMPTIONS.md`).

The unit economics model derives the same wall independently: fixed
per-loan costs (servicing plus acquisition) divided by the risk-adjusted fee
margin gives a breakeven ticket of Rs 22,857, within 9% of the industry
figure, from cost structure alone rather than market convention. Two
unrelated calculations landing in the same place is the strongest signal in
this repo.

**Consequence:** every scenario at this book's actual size loses money, and
the optimistic case (higher disbursal, lower default) loses more than the
pessimistic one, because more loans just multiply a fixed per-loan loss.
Disbursal rate is the wrong lever right now.

| avg portfolio | net revenue / loan | viable? |
|---|---|---|
| Rs 1,800 (today) | -Rs 232 | No |
| Rs 10,000 | -Rs 194 | No |
| Rs 50,000 | -Rs 9 | No |
| Rs 75,000 | +Rs 107 | Yes |

Viability starts around a Rs 52,000 average portfolio, about 29x the Rs
1,800 average this scenario assumes today, and about 60x the book's actual
median portfolio (Rs 862). That's a multi-year maturity question, not a
targeting or disbursal-rate question.

## Who to target, when it's viable

The propensity model (logistic regression, AUC 0.870, 3.5x lift at the top
100 users) says clearly: consistency dominates collateral.

| feature | effect per 1 SD |
|---|---|
| Active-day ratio | 5.06x more likely |
| Streak breaks | 2.37x |
| Tier-3 city | 1.30x |
| Portfolio value | 1.07x |
| Tenure | 0.60x (younger accounts more likely) |

A user who invests reliably every day is a fundamentally different, more
fundable prospect than one who invests erratically, five times more
predictive than raw portfolio size. That's the direct payoff of feeding
Cadence's streak output into this model instead of scoring on AUM alone: two
users with identical portfolios can have very different LAMF propensity, and
consistency is what tells them apart.

**Practical read:** when portfolios mature enough to clear the real minimum,
target the high-consistency segment first, not the highest-AUM segment. Note
also that tier-3 users score higher on candidacy (1.30x) but showed the
weakest nudge response in the segment breakdown below, worth resolving
before acting on the ranking alone.

## The nudge works today, not just later

Independent of the collateral constraint: when a user hits a cash need and is
shown "borrow instead of withdraw", it measurably reduces SIP breakage.

- 45.2% to 33.4% breakage, control vs. treatment, among eligible users
- -11.8 percentage points (95% CI -19.3 to -4.0), p = 0.0029
- Arms were balanced pre-treatment (p = 0.88 on eligibility, p = 0.42 on need
  size), so this is a real causal estimate, not selection

This doesn't depend on the ticket-size problem. It's a retention mechanism
that works today, for whichever users are eligible, however few that is right
now.

## What this means for the roadmap

1. Don't push disbursal rate on this book. It burns money faster.
2. Do keep nudging eligible users toward borrowing over withdrawing. It's
   working and it's free of the collateral constraint.
3. Revisit at scale. Re-run this pipeline once average portfolios approach
   Rs 50-75k (older cohorts, larger SIP tickets, or both), and target by
   consistency first when that day comes.
4. Every number above is built on named assumptions, most consequentially a
   1.5% origination fee and Rs 180/loan servicing cost, both `ASSUMED`, not
   sourced. See `ASSUMPTIONS.md` before repeating any of this outside this
   repo.
