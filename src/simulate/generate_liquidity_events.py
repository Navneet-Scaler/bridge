"""Portfolio growth, liquidity events, and the borrow-vs-withdraw counterfactual.

This module builds the world Bridge analyses. It does three things in order, and
the ordering matters because each stage depends on the last:

**1. Grow every portfolio from Cadence's transaction history.**
    Portfolio value is not invented here. It is compounded forward from the
    *actual* successful daily SIP contributions in Cadence's
    ``v_clean_transactions``, so a user who invested consistently ends up with a
    larger collateral base than one who invested erratically. That link is the
    entire reason the two projects share a database: investing consistency is not
    just a model feature bolted on later, it mechanically determines how much a
    user can borrow.

    Growth is geometric Brownian motion, not a straight line. A portfolio that
    compounds deterministically makes loan-to-value risk vanish by construction,
    and LTV drift is the actual risk in a lending-against-securities product —
    collateral falling while the loan stays fixed is what triggers a margin call.
    A deterministic simulation would let the economics model report a risk number
    that the data could never have produced.

**2. Generate liquidity needs, with structure rather than noise.**
    Each user draws a per-day hazard of needing cash that varies by city tier and
    tenure. If the hazard were flat, the propensity model in phase 4 would have
    nothing to find and would be measuring its own regularisation. The structure
    planted here is what the model should recover — and ``sim_liquidity_events``
    records it so the recovery can be checked rather than assumed.

**3. Resolve each event down one of two branches.**
    Every liquidity event is assigned to an arm *at the event*, independently of
    the user's attributes:

    * **control** — no nudge. The user withdraws.
    * **treatment** — shown "borrow against your portfolio instead". If they are
      eligible and they take it, they borrow and the SIP survives. Otherwise they
      withdraw like the control arm.

    Eligibility is recorded separately from resolution on purpose. An ineligible
    treatment user *cannot* borrow no matter how good the nudge is, so pooling
    them with eligible users would dilute the measured effect toward zero and
    understate the intervention on the population it can actually reach. Phase 6
    needs both the intention-to-treat estimate and the effect among the eligible,
    and only recording the two fields separately makes that possible.

A note on what is and is not an assumption
------------------------------------------
The rate (9.99%) and the maximum loan-to-value (80%) are figures the product
itself advertises. Everything else in the CONSTANTS block below — expected market
return, volatility, how often people need cash, how many take the loan when
offered — is an **assumption made for simulation purposes**. They are named
constants rather than inline numbers so they can be found, changed, and argued
with, and every one of them is documented in ASSUMPTIONS.md.

Crucially, these constants determine what the simulated data looks like; they are
*not* findings. The uptake rate below is an input to the simulation, so the fact
that phase 6 recovers something close to it is a check that the pipeline works —
not evidence about how real users behave.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from src import db

logger = logging.getLogger(__name__)


# =============================================================================
# ASSUMPTIONS — every one of these is documented in ASSUMPTIONS.md
# =============================================================================

# --- Product facts (advertised by the product; not assumptions about margin) ---
INTEREST_RATE_PCT = 9.99  # annual rate charged to the borrower
MAX_LTV = 0.80  # borrow up to 80% of portfolio value

# --- Market assumptions (simulation inputs) ---
# ~15% nominal annual return is the figure the product's own "keep compounding"
# pitch implies. Indian equity mutual funds have historically run in this band
# over long horizons, but it is an ASSUMPTION, not a promise or a forecast.
ANNUAL_RETURN = 0.15
# Annualised volatility. Without this, collateral never falls and LTV risk is
# structurally impossible — the simulation would beg the question the risk
# section is supposed to answer.
ANNUAL_VOLATILITY = 0.14

# --- Eligibility screen (a product rule we are proposing, not observed) ---
#
# READ THIS BEFORE CHANGING THE NUMBER. It is the single most consequential
# constant in the repo, and the constraint behind it is the most important thing
# this project surfaces.
#
# Loan-against-mutual-funds products in India typically set a minimum ticket
# around Rs 25,000, because the cost to originate and service a loan is roughly
# fixed and a Rs 4,000 loan cannot carry it. Applying that Rs 25,000 minimum to
# this cohort makes *every single user ineligible*: a Rs 21/day micro-SIP book
# one year old has a median lifetime contribution of about Rs 780 and its single
# largest portfolio is under Rs 11,000.
#
# That is not a simulation artifact to be tuned away — it is the actual business
# constraint. A daily micro-SIP book does not have enough collateral to support
# LAMF at market-standard minimums in year one. The cross-sell becomes viable
# either as portfolios mature over years two and three, or by running a
# deliberately sub-scale ticket that the standard unit economics do not support.
#
# The threshold below is set at Rs 2,000 — roughly 12x under the market standard
# — purely so the downstream analysis has a population to work with. Every
# revenue figure derived from it is therefore an upper bound on a book that could
# not actually be written at these economics today. MEMO.md leads with this, and
# the economics model treats portfolio maturity as the binding input rather than
# disbursal rate.
MIN_PORTFOLIO_FOR_LAMF = 2_000.0
MARKET_STANDARD_MIN_TICKET = 25_000.0  # what the industry actually requires, for contrast
MIN_TENURE_DAYS_FOR_LAMF = 90  # account age; a brand-new account is not a lending relationship

# --- Behavioural assumptions (simulation inputs) ---
# Baseline per-day probability that a user needs cash. Calibrated so a typical
# user faces roughly one liquidity event a year, which keeps event counts in a
# plausible range rather than targeting any published statistic.
BASE_LIQUIDITY_HAZARD = 1 / 365

# Multipliers on that hazard. These plant the structure the propensity model is
# meant to find. Tier 3 users are modelled as facing more cash crunches and
# holding thinner buffers; longer-tenured users as somewhat more settled.
CITY_TIER_LIQUIDITY_MULTIPLIER = {"tier_1": 0.80, "tier_2": 1.00, "tier_3": 1.35}
TENURE_LIQUIDITY_MULTIPLIER = {"new": 1.25, "established": 1.00, "veteran": 0.85}

# Size of the cash need, as a fraction of portfolio value.
NEED_FRACTION_MEAN = 0.30
NEED_FRACTION_SD = 0.12
NEED_FRACTION_MIN = 0.05
NEED_FRACTION_MAX = 0.95

# Of eligible users shown the nudge, the share who borrow instead of withdrawing.
# This is the intervention's effect size and is a pure ASSUMPTION — no public
# figure exists for BlinkMoney's LAMF conversion. Treat every downstream revenue
# number as conditional on it, which is why the economics model runs a scenario
# table across it rather than quoting a single answer.
NUDGE_UPTAKE_RATE = 0.34

# A withdrawal does not always kill the SIP. Taking out a small slice is
# survivable; liquidating most of the portfolio generally is not. Modelled as a
# floor plus a term rising with the fraction withdrawn.
SIP_BREAK_BASE = 0.35
SIP_BREAK_SLOPE = 0.55

# Loan terms.
LOAN_TENURE_CHOICES = [6, 12, 18, 24]
LOAN_TENURE_WEIGHTS = [0.18, 0.46, 0.22, 0.14]

# Share of disbursed loans that end in default. LAMF is over-collateralised by
# liquid mutual fund units that can be sold on a margin call, so realised losses
# are materially lower than in unsecured consumer credit. The 1.2% here is an
# ASSUMPTION chosen to sit in that low band; it is not sourced from BlinkMoney
# and not taken from any published portfolio.
LOAN_DEFAULT_RATE = 0.012
# Share of loans that have run to completion within the observation window.
LOAN_CLOSED_RATE = 0.22

# Not every eligible application is approved: KYC re-checks, collateral holds and
# lender-side limits all bite. Applied to eligible applications only.
APPROVAL_RATE_GIVEN_ELIGIBLE = 0.94


@dataclass
class SimulationConfig:
    """Run parameters, read from the environment so runs are reproducible."""

    start_date: date
    end_date: date
    random_seed: int

    @classmethod
    def from_env(cls) -> SimulationConfig:
        """Build config from ``.env``.

        The seed must match Cadence's, or the portfolio trajectories stop lining
        up with the transaction history they are compounded from.
        """
        return cls(
            start_date=date.fromisoformat(os.getenv("SIM_START_DATE", "2025-01-01")),
            end_date=date.fromisoformat(os.getenv("SIM_END_DATE", "2025-12-31")),
            random_seed=int(os.getenv("SIM_RANDOM_SEED", "42")),
        )


# =============================================================================
# 1. Portfolio growth
# =============================================================================


def load_contribution_matrix(config: SimulationConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read Cadence's successful contributions and pivot to a user x day matrix.

    Reads ``v_clean_transactions`` rather than the raw table, so Cadence's
    deduplication and signup-date validity rules are already applied — Bridge
    does not get its own opinion about which transactions are real.

    Returns:
        ``(contributions, users)`` where contributions is a dense
        users x days frame of rupees contributed (zero on non-investing days).
    """
    users = db.read_sql(
        """
        SELECT user_id, signup_date, city_tier, kyc_status, kyc_completed_at
        FROM users
        ORDER BY user_id
        """
    )
    if users.empty:
        raise RuntimeError(
            "no users found — Bridge extends Cadence's database. Run Cadence's "
            "generator (make run-sim there) before simulating portfolios."
        )

    txns = db.read_sql(
        """
        SELECT user_id, txn_date, COALESCE(amount, 0) AS amount
        FROM v_clean_transactions
        WHERE status = 'success'
        """
    )
    if txns.empty:
        raise RuntimeError("no successful transactions found — run Cadence's generator first")

    txns["txn_date"] = pd.to_datetime(txns["txn_date"])
    calendar = pd.date_range(config.start_date, config.end_date, freq="D")

    contributions = (
        txns.pivot_table(index="user_id", columns="txn_date", values="amount", aggfunc="sum")
        .reindex(index=users["user_id"], columns=calendar)
        .fillna(0.0)
    )
    logger.info(
        "loaded contributions for %s users across %s days",
        f"{len(contributions):,}",
        f"{len(calendar):,}",
    )
    return contributions, users


def grow_portfolios(
    contributions: pd.DataFrame, config: SimulationConfig, rng: np.random.Generator
) -> pd.DataFrame:
    """Compound daily contributions into a mark-to-market portfolio value.

    Each day: yesterday's balance is scaled by that day's market move, then the
    day's contribution is added. Contributing at the end of the day means a
    contribution never earns a return on the day it was made, which is the
    conservative convention and avoids a one-day free gain.

    The market move is geometric Brownian motion with a **market-wide** shock
    each day, not an independent draw per user. Every portfolio holding broadly
    the same mutual funds falls on the same bad day; independent per-user noise
    would diversify that away across 5,000 users and make aggregate collateral
    look far more stable than it is. Since the whole point of the risk section is
    what happens to the loan book when collateral drops, that correlation has to
    be present.

    Returns:
        A users x days frame of portfolio values.
    """
    n_days = contributions.shape[1]
    dt = 1 / 365
    drift = (ANNUAL_RETURN - 0.5 * ANNUAL_VOLATILITY**2) * dt
    diffusion = ANNUAL_VOLATILITY * np.sqrt(dt)

    # One shock per day, shared across all users — market moves are common.
    daily_factor = np.exp(drift + diffusion * rng.standard_normal(n_days))

    contrib = contributions.to_numpy(dtype=float)
    values = np.empty_like(contrib)
    balance = np.zeros(contrib.shape[0], dtype=float)
    for day in range(n_days):
        balance = balance * daily_factor[day] + contrib[:, day]
        values[:, day] = balance

    portfolios = pd.DataFrame(values, index=contributions.index, columns=contributions.columns)
    logger.info(
        "grew portfolios: median final value %s, p90 %s",
        f"{portfolios.iloc[:, -1].median():,.0f}",
        f"{portfolios.iloc[:, -1].quantile(0.9):,.0f}",
    )
    return portfolios


# =============================================================================
# 2. Liquidity events
# =============================================================================


def _tenure_band(days: int) -> str:
    """Bucket account age. Bands, not a continuous term, so the planted effect is
    legible in a crosstab as well as recoverable by a model."""
    if days < 120:
        return "new"
    if days < 240:
        return "established"
    return "veteran"


def generate_liquidity_events(
    users: pd.DataFrame,
    portfolios: pd.DataFrame,
    config: SimulationConfig,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Draw cash-need events per user-day from a segment-varying hazard.

    The hazard is the baseline scaled by the user's city tier and current tenure
    band, so the propensity model in phase 4 has real structure to recover rather
    than noise. Events are only drawn once a user has a portfolio worth borrowing
    against; a cash need with no collateral is not a LAMF opportunity and would
    just add rows that no arm could ever resolve differently.

    Returns:
        One row per event with the user, date, amount needed, and the portfolio
        value at that moment.
    """
    calendar = portfolios.columns
    signup = pd.to_datetime(users.set_index("user_id")["signup_date"])
    tiers = users.set_index("user_id")["city_tier"]

    tier_mult = tiers.map(CITY_TIER_LIQUIDITY_MULTIPLIER).fillna(1.0).to_numpy()[:, None]

    # Tenure in days for every user-day, then mapped to a band multiplier.
    days_since_signup = (
        (calendar.to_numpy()[None, :] - signup.reindex(portfolios.index).to_numpy()[:, None])
        .astype("timedelta64[D]")
        .astype(int)
    )

    tenure_mult = np.ones_like(days_since_signup, dtype=float)
    tenure_mult[days_since_signup < 120] = TENURE_LIQUIDITY_MULTIPLIER["new"]
    tenure_mult[(days_since_signup >= 120) & (days_since_signup < 240)] = (
        TENURE_LIQUIDITY_MULTIPLIER["established"]
    )
    tenure_mult[days_since_signup >= 240] = TENURE_LIQUIDITY_MULTIPLIER["veteran"]

    hazard = BASE_LIQUIDITY_HAZARD * tier_mult * tenure_mult

    values = portfolios.to_numpy()
    # No event before the account exists, or before there is meaningful collateral.
    hazard = np.where((days_since_signup >= 0) & (values > MIN_PORTFOLIO_FOR_LAMF * 0.5), hazard, 0)

    fired = rng.random(hazard.shape) < hazard
    user_idx, day_idx = np.nonzero(fired)
    if len(user_idx) == 0:
        raise RuntimeError("no liquidity events generated — check the hazard constants")

    portfolio_at_event = values[user_idx, day_idx]
    need_fraction = np.clip(
        rng.normal(NEED_FRACTION_MEAN, NEED_FRACTION_SD, size=len(user_idx)),
        NEED_FRACTION_MIN,
        NEED_FRACTION_MAX,
    )

    events = pd.DataFrame(
        {
            "user_id": portfolios.index.to_numpy()[user_idx],
            "event_date": calendar[day_idx],
            "portfolio_value": portfolio_at_event,
            "needed_amount": np.round(portfolio_at_event * need_fraction, 2),
            "days_since_signup": days_since_signup[user_idx, day_idx],
        }
    ).sort_values(["user_id", "event_date"], ignore_index=True)

    events["tenure_band"] = events["days_since_signup"].map(_tenure_band)
    logger.info(
        "generated %s liquidity events across %s users",
        f"{len(events):,}",
        f"{events['user_id'].nunique():,}",
    )
    return events


# =============================================================================
# 3. The counterfactual branch
# =============================================================================


def resolve_events(
    events: pd.DataFrame,
    users: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Assign each event an arm and resolve it into a withdrawal or a loan.

    Arm assignment is a coin flip **at the event**, independent of every user
    attribute. That independence is what licenses a causal reading of the phase 6
    comparison: if arms were assigned on anything correlated with stickiness, the
    measured effect would be selection, not the nudge.

    Eligibility is evaluated at event time from the three screens the product
    would actually apply — KYC verified, enough collateral, enough account age —
    and stored alongside the resolution so the two never get conflated.
    """
    kyc = users.set_index("user_id")[["kyc_status", "kyc_completed_at"]]
    joined = events.join(kyc, on="user_id")

    eligible = (
        (joined["kyc_status"] == "verified")
        & (joined["portfolio_value"] >= MIN_PORTFOLIO_FOR_LAMF)
        & (joined["days_since_signup"] >= MIN_TENURE_DAYS_FOR_LAMF)
    )

    arm = np.where(rng.random(len(joined)) < 0.5, "treatment", "control")
    offered = (arm == "treatment") & eligible.to_numpy()
    took_loan = offered & (rng.random(len(joined)) < NUDGE_UPTAKE_RATE)

    resolved = joined.copy()
    resolved["arm"] = arm
    resolved["was_eligible"] = eligible.to_numpy()
    resolved["resolution"] = np.where(took_loan, "borrowed", "withdrew")

    # Breakage only applies to withdrawals: borrowing leaves the SIP untouched,
    # which is the entire mechanism the product is selling.
    withdrew = resolved["resolution"] == "withdrew"
    fraction = (resolved["needed_amount"] / resolved["portfolio_value"]).clip(0, 1)
    break_prob = np.clip(SIP_BREAK_BASE + SIP_BREAK_SLOPE * fraction, 0, 1)
    resolved["sip_broken"] = withdrew & (rng.random(len(resolved)) < break_prob)

    logger.info(
        "resolved %s events: %s borrowed, %s withdrew (%s broke the SIP)",
        f"{len(resolved):,}",
        f"{int((resolved['resolution'] == 'borrowed').sum()):,}",
        f"{int(withdrew.sum()):,}",
        f"{int(resolved['sip_broken'].sum()):,}",
    )
    return resolved


def build_tables(resolved: pd.DataFrame, rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    """Split resolved events into the five production-shaped tables.

    Applications are written for every *eligible treatment* event, not only the
    ones that converted. A funnel that only records its successes cannot show a
    drop-off, and the drop-off is what the pipeline dashboard exists to surface.
    """
    treatment_eligible = resolved[
        (resolved["arm"] == "treatment") & resolved["was_eligible"]
    ].copy()

    # --- borrow_nudges: every treatment event gets a nudge, eligible or not ---
    treatment = resolved[resolved["arm"] == "treatment"]
    nudges = pd.DataFrame(
        {
            "user_id": treatment["user_id"].to_numpy(),
            "triggered_by": "liquidity_signal",
            "sent_date": treatment["event_date"].to_numpy(),
            "resulted_in_loan": (treatment["resolution"] == "borrowed").to_numpy(),
        }
    )

    # --- loan_applications: one per eligible, nudged event ---
    borrowed = treatment_eligible["resolution"] == "borrowed"
    # Requested amount is capped at the product's max LTV.
    requested = np.minimum(
        treatment_eligible["needed_amount"].to_numpy(),
        treatment_eligible["portfolio_value"].to_numpy() * MAX_LTV,
    )
    approved = borrowed.to_numpy() | (
        rng.random(len(treatment_eligible)) < APPROVAL_RATE_GIVEN_ELIGIBLE
    )
    applications = pd.DataFrame(
        {
            "application_id": np.arange(1, len(treatment_eligible) + 1),
            "user_id": treatment_eligible["user_id"].to_numpy(),
            "applied_date": treatment_eligible["event_date"].to_numpy(),
            "requested_amount": np.round(requested, 2),
            "eligible": True,
            "approved": approved,
        }
    )

    # --- loans: only for applications that converted ---
    loan_rows = applications[borrowed.to_numpy()].copy()
    n_loans = len(loan_rows)
    status = np.full(n_loans, "active", dtype=object)
    roll = rng.random(n_loans)
    status[roll < LOAN_DEFAULT_RATE] = "default"
    status[(roll >= LOAN_DEFAULT_RATE) & (roll < LOAN_DEFAULT_RATE + LOAN_CLOSED_RATE)] = "closed"

    loans = pd.DataFrame(
        {
            "application_id": loan_rows["application_id"].to_numpy(),
            "disbursed_amount": loan_rows["requested_amount"].to_numpy(),
            "disbursal_date": loan_rows["applied_date"].to_numpy(),
            "interest_rate": INTEREST_RATE_PCT,
            "tenure_months": rng.choice(LOAN_TENURE_CHOICES, size=n_loans, p=LOAN_TENURE_WEIGHTS),
            "status": status,
        }
    )

    # --- withdrawal_events: everything that did not borrow ---
    withdrew = resolved[resolved["resolution"] == "withdrew"]
    withdrawals = pd.DataFrame(
        {
            "user_id": withdrew["user_id"].to_numpy(),
            "event_date": withdrew["event_date"].to_numpy(),
            "amount": withdrew["needed_amount"].to_numpy(),
            "sip_broken": withdrew["sip_broken"].to_numpy(),
        }
    )

    # --- sim_liquidity_events: ground truth, kept apart from the above ---
    sim_events = pd.DataFrame(
        {
            "user_id": resolved["user_id"].to_numpy(),
            "event_date": resolved["event_date"].to_numpy(),
            "needed_amount": resolved["needed_amount"].to_numpy(),
            "arm": resolved["arm"].to_numpy(),
            "was_eligible": resolved["was_eligible"].to_numpy(),
            "resolution": resolved["resolution"].to_numpy(),
        }
    )

    return {
        "withdrawal_events": withdrawals,
        "loan_applications": applications,
        "loans": loans,
        "borrow_nudges": nudges,
        "sim_liquidity_events": sim_events,
    }


def portfolio_snapshot_frame(portfolios: pd.DataFrame) -> pd.DataFrame:
    """Melt the users x days matrix into the long form the table stores."""
    long = portfolios.stack().reset_index()
    long.columns = ["user_id", "snapshot_date", "portfolio_value"]
    long["portfolio_value"] = long["portfolio_value"].round(2)
    return long


# =============================================================================
# Loading
# =============================================================================


def truncate_bridge_tables() -> None:
    """Clear Bridge's tables only, leaving Cadence's untouched.

    Deliberately does **not** cascade to ``users``. Bridge re-runs must never
    renumber Cadence's user ids: every Bridge row keys off them, and a
    ``RESTART IDENTITY`` on the parent would orphan the lot.
    """
    db.execute(
        "TRUNCATE sim_liquidity_events, borrow_nudges, loans, loan_applications, "
        "withdrawal_events, portfolio_snapshots RESTART IDENTITY"
    )
    logger.info("truncated Bridge tables (Cadence's are untouched)")


def load(tables: dict[str, pd.DataFrame], snapshots: pd.DataFrame) -> None:
    """COPY every generated frame into Postgres, parents before children."""
    db.bulk_load(snapshots, "portfolio_snapshots", ["user_id", "snapshot_date", "portfolio_value"])
    db.bulk_load(
        tables["withdrawal_events"],
        "withdrawal_events",
        ["user_id", "event_date", "amount", "sip_broken"],
    )
    db.bulk_load(
        tables["loan_applications"],
        "loan_applications",
        ["application_id", "user_id", "applied_date", "requested_amount", "eligible", "approved"],
    )
    db.reset_sequence("loan_applications", "application_id")
    db.bulk_load(
        tables["loans"],
        "loans",
        [
            "application_id",
            "disbursed_amount",
            "disbursal_date",
            "interest_rate",
            "tenure_months",
            "status",
        ],
    )
    db.bulk_load(
        tables["borrow_nudges"],
        "borrow_nudges",
        ["user_id", "triggered_by", "sent_date", "resulted_in_loan"],
    )
    db.bulk_load(
        tables["sim_liquidity_events"],
        "sim_liquidity_events",
        ["user_id", "event_date", "needed_amount", "arm", "was_eligible", "resolution"],
    )


def collateral_profile(portfolios: pd.DataFrame) -> pd.DataFrame:
    """How many users clear each candidate collateral threshold, at window close.

    This is the table the memo's headline comes from. It exists because the
    eligibility threshold is not a tuning knob but the binding constraint on the
    whole product: at the market-standard Rs 25,000 minimum ticket, a one-year
    micro-SIP book has essentially no addressable population, and any revenue
    model built on top of it would be describing a book that cannot be written.

    Reporting the full ladder rather than a single number keeps that visible —
    a reader can see exactly where the population disappears instead of taking
    the chosen threshold on trust.
    """
    final = portfolios.iloc[:, -1]
    thresholds = [500, 1_000, 2_000, 5_000, 10_000, MARKET_STANDARD_MIN_TICKET]
    rows = [
        {
            "min_portfolio": threshold,
            "users_eligible": int((final >= threshold).sum()),
            "pct_of_book": round(100 * float((final >= threshold).mean()), 2),
            "collateral_covered": round(float(final[final >= threshold].sum()), 2),
        }
        for threshold in thresholds
    ]
    return pd.DataFrame(rows)


def summarise(resolved: pd.DataFrame) -> pd.DataFrame:
    """The headline treatment/control contrast, logged so a bad run is obvious.

    This is a sanity check on the simulation, not a result. Phase 6 does the
    actual inference with a significance test; if these two rows look identical,
    something upstream is broken and there is no point proceeding.
    """
    eligible = resolved[resolved["was_eligible"]]
    return (
        eligible.groupby("arm")
        .agg(
            events=("user_id", "size"),
            borrowed=("resolution", lambda s: int((s == "borrowed").sum())),
            sip_broken=("sip_broken", "sum"),
        )
        .assign(
            uptake_rate=lambda d: (d["borrowed"] / d["events"]).round(4),
            breakage_rate=lambda d: (d["sip_broken"] / d["events"]).round(4),
        )
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Simulate portfolios, liquidity events, and the borrow/withdraw split."
    )
    parser.add_argument("--seed", type=int, default=None, help="override SIM_RANDOM_SEED")
    parser.add_argument("--no-load", action="store_true", help="generate but do not write")
    args = parser.parse_args()

    config = SimulationConfig.from_env()
    if args.seed is not None:
        config.random_seed = args.seed

    logger.info(
        "simulating %s to %s (seed=%d)", config.start_date, config.end_date, config.random_seed
    )
    rng = np.random.default_rng(config.random_seed)

    contributions, users = load_contribution_matrix(config)
    portfolios = grow_portfolios(contributions, config, rng)

    profile = collateral_profile(portfolios)
    logger.info(
        "addressable population by collateral threshold:\n%s", profile.to_string(index=False)
    )
    at_market_standard = profile.iloc[-1]
    if at_market_standard["users_eligible"] == 0:
        logger.warning(
            "NO user clears the Rs %s market-standard minimum ticket. LAMF is not "
            "viable on this book at standard economics; see MEMO.md.",
            f"{MARKET_STANDARD_MIN_TICKET:,.0f}",
        )

    events = generate_liquidity_events(users, portfolios, config, rng)
    resolved = resolve_events(events, users, rng)
    tables = build_tables(resolved, rng)
    snapshots = portfolio_snapshot_frame(portfolios)

    logger.info("treatment vs control among eligible events:\n%s", summarise(resolved).to_string())

    if args.no_load:
        logger.info("--no-load set; nothing written")
        return

    truncate_bridge_tables()
    load(tables, snapshots)
    logger.info("done")


if __name__ == "__main__":
    main()
