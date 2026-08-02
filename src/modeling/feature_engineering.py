"""Feature set for the LAMF propensity model.

The hard problem here is not which features to compute — it is making sure none
of them know the future. Every design decision below exists to prevent leakage,
because a leaky propensity model does not fail loudly: it reports a beautiful
AUC and then performs at chance in production, and by then it has already been
used to decide who to lend to.

The temporal split
------------------
Features and outcome are computed from **disjoint time windows**:

    |<--- feature window --->|<--- outcome window --->|
    Jan 1              Jun 30 |                Dec 31
                           CUTOFF

Everything the model sees is computed strictly on or before ``FEATURE_CUTOFF``.
The thing it predicts happens strictly after. This is stronger than auditing a
feature list by eye, because it makes whole *categories* of leakage structurally
impossible rather than individually remembered.

It matters most for the consistency features, where the leak would be subtle and
backwards. A user who withdraws and breaks their SIP stops investing — so their
active-day ratio measured over the full year is depressed *by the very outcome
being predicted*. Train on that and the model learns "users with poor
consistency are LAMF candidates", which is the causal arrow pointing the wrong
way. Cutting the feature window at June makes that impossible.

The target variable
-------------------
The PRD offers two candidate definitions. This module uses:

    **had at least one eligible liquidity event after the cutoff**

rather than "took a loan when eligible and offered". The reasons, since this is
a judgment call and not an obvious one:

* It is observable for the *whole* user base. The alternative is only defined
  for users who happened to land in the treatment arm and were shown a nudge —
  around 300 events — which is both a small positive class and a population
  selected by a coin flip rather than by anything about the user.
* It matches the decision it will be used for. The question a founder asks is
  "who should we target for LAMF first", and targeting acts on who will present
  a fundable opportunity, not on who converts once asked.

What it is **not**: a measure of willingness to borrow. This target conflates
"will need cash" with "will qualify to borrow". A user scoring highly is someone
a LAMF opportunity is likely to arise for — not someone we know wants a loan.
That distinction belongs in the memo, because acting on the score as if it meant
intent would overstate expected conversion.

The cross-project link
----------------------
``longest_streak``, ``streak_breaks`` and ``active_day_ratio`` come from
Cadence's ``user_streaks`` — the same gaps-and-islands output its retention
analysis is built on, filtered to the feature window. This is the point of the
two projects sharing a database: consistency is not re-derived here with a
subtly different definition, it is read from the one place that owns it.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from src import db

logger = logging.getLogger(__name__)

# End of the feature window. Chosen as the midpoint of the observation year:
# early enough that six months of outcomes remain to predict, late enough that
# portfolios have grown to where eligibility is attainable. Both halves matter —
# cut it too early and almost nobody is eligible in the feature window; too late
# and there is no outcome left to observe.
FEATURE_CUTOFF = date(2025, 6, 30)

# Columns that must NEVER become features. Each is knowable only after the
# outcome it would be predicting, so including one would leak the answer.
# tests/test_feature_engineering.py asserts none of these reach the model.
FORBIDDEN_FEATURES = frozenset(
    {
        "resulted_in_loan",  # nudge outcome
        "resolution",  # borrowed vs withdrew
        "was_eligible",  # eligibility at the event being predicted
        "sip_broken",  # the downstream consequence
        "approved",  # lender decision, post-application
        "disbursed_amount",  # exists only if the loan happened
        "loan_status",
        "needed_amount",  # size of the very event being predicted
        "event_date",
        "target",
    }
)

FEATURE_COLUMNS = [
    "portfolio_value",
    "tenure_days",
    "successful_days",
    "active_day_ratio",
    "longest_streak",
    "streak_breaks",
    "kyc_speed_days",
    "kyc_incomplete",
    "city_tier_tier_2",
    "city_tier_tier_3",
]


# =============================================================================
# Pure computation — no database, so leakage is testable on fixtures
# =============================================================================


def build_features(
    users: pd.DataFrame,
    transactions: pd.DataFrame,
    streaks: pd.DataFrame,
    portfolios: pd.DataFrame,
    cutoff: date = FEATURE_CUTOFF,
) -> pd.DataFrame:
    """Compute one feature row per active user, using only pre-cutoff data.

    Every input frame is filtered to the feature window *inside this function*
    rather than by the caller. That is deliberate: a caller who forgets to filter
    would otherwise silently produce a leaky feature set, and the filtering is
    the one thing that must not be optional.

    Args:
        users: user_id, signup_date, city_tier, kyc_status, kyc_completed_at.
        transactions: user_id, txn_date, status (Cadence's clean view).
        streaks: user_id, streak_end, streak_length, is_censored (Cadence).
        portfolios: user_id, snapshot_date, portfolio_value.
        cutoff: last day the model is allowed to know about.

    Returns:
        One row per user active at the cutoff, indexed by user_id.
    """
    cutoff_ts = pd.Timestamp(cutoff)

    users = users.copy()
    users["signup_date"] = pd.to_datetime(users["signup_date"])
    users["kyc_completed_at"] = pd.to_datetime(users["kyc_completed_at"])

    # --- population: signed up and actually investing before the cutoff -------
    txns = transactions.copy()
    txns["txn_date"] = pd.to_datetime(txns["txn_date"])
    past_txns = txns[(txns["txn_date"] <= cutoff_ts) & (txns["status"] == "success")]

    active_users = set(past_txns["user_id"].unique())
    population = users[
        (users["signup_date"] <= cutoff_ts) & (users["user_id"].isin(active_users))
    ].copy()

    if population.empty:
        raise ValueError(f"no users were active on or before {cutoff}")

    frame = population.set_index("user_id")
    # Inclusive of both signup day and cutoff day. Elapsed-days arithmetic gives
    # a user who signed up yesterday a tenure of 1 while they may already have
    # invested on two distinct dates, which pushes active_day_ratio above 1 — a
    # nonsense value for a "share of days active" feature and a confusing one to
    # read a coefficient off.
    frame["tenure_days"] = (cutoff_ts - frame["signup_date"]).dt.days + 1

    # --- investing behaviour, feature window only ----------------------------
    frame["successful_days"] = (
        past_txns.groupby("user_id")["txn_date"].nunique().reindex(frame.index).fillna(0)
    )
    # Ratio of days invested to days the account has existed. Guards against
    # tenure zero, which would otherwise divide by zero on a same-day signup.
    frame["active_day_ratio"] = frame["successful_days"] / frame["tenure_days"].clip(lower=1)

    # --- consistency, from Cadence's streak output ---------------------------
    # Only streaks that had already ENDED by the cutoff. A streak still running
    # at the cutoff has a length that keeps growing afterwards, so using it would
    # read post-cutoff behaviour into a pre-cutoff feature.
    past_streaks = streaks.copy()
    past_streaks["streak_end"] = pd.to_datetime(past_streaks["streak_end"])
    past_streaks = past_streaks[past_streaks["streak_end"] <= cutoff_ts]

    frame["longest_streak"] = (
        past_streaks.groupby("user_id")["streak_length"].max().reindex(frame.index).fillna(0)
    )
    frame["streak_breaks"] = (
        past_streaks[~past_streaks["is_censored"].astype(bool)]
        .groupby("user_id")
        .size()
        .reindex(frame.index)
        .fillna(0)
    )

    # --- collateral at the cutoff --------------------------------------------
    port = portfolios.copy()
    port["snapshot_date"] = pd.to_datetime(port["snapshot_date"])
    port = port[port["snapshot_date"] <= cutoff_ts]
    latest = port.sort_values("snapshot_date").groupby("user_id").last()
    frame["portfolio_value"] = latest["portfolio_value"].reindex(frame.index).fillna(0.0)

    # --- KYC speed -----------------------------------------------------------
    # Completion after the cutoff counts as "not yet complete": at prediction
    # time we would not know it was coming.
    kyc_completed_in_window = frame["kyc_completed_at"].where(
        frame["kyc_completed_at"] <= cutoff_ts
    )
    kyc_speed = (kyc_completed_in_window - frame["signup_date"]).dt.days
    frame["kyc_incomplete"] = kyc_speed.isna().astype(int)
    # Impute the slow extreme rather than the mean: "never verified" is the far
    # end of slow, not a typical user. The flag above keeps the imputation
    # distinguishable from a real measurement.
    frame["kyc_speed_days"] = kyc_speed.fillna(kyc_speed.max() if kyc_speed.notna().any() else 0)

    # --- city tier, one-hot with tier_1 as reference -------------------------
    frame["city_tier_tier_2"] = (frame["city_tier"] == "tier_2").astype(int)
    frame["city_tier_tier_3"] = (frame["city_tier"] == "tier_3").astype(int)

    features = frame[FEATURE_COLUMNS].astype(float)
    logger.info(
        "built %s feature rows as of %s (%d features)",
        f"{len(features):,}",
        cutoff,
        len(FEATURE_COLUMNS),
    )
    return features


def build_target(events: pd.DataFrame, index: pd.Index, cutoff: date = FEATURE_CUTOFF) -> pd.Series:
    """Label each user 1 if an eligible liquidity event occurred after the cutoff.

    Strictly ``>`` the cutoff, not ``>=``. An event on the cutoff day itself is
    ambiguous — the features are stated "as of" that day — and one day of overlap
    is exactly the kind of off-by-one that quietly inflates a score.
    """
    ev = events.copy()
    ev["event_date"] = pd.to_datetime(ev["event_date"])
    future = ev[(ev["event_date"] > pd.Timestamp(cutoff)) & ev["was_eligible"].astype(bool)]

    target = pd.Series(0, index=index, name="target", dtype=int)
    target.loc[target.index.isin(future["user_id"].unique())] = 1

    rate = float(target.mean())
    logger.info(
        "target: %s positives of %s users (%.1f%% base rate)",
        f"{int(target.sum()):,}",
        f"{len(target):,}",
        100 * rate,
    )
    if rate > 0.5:
        logger.warning("positive class is the majority — check the cutoff")
    return target


def assert_no_leakage(features: pd.DataFrame) -> None:
    """Fail loudly if a forbidden column reached the feature matrix.

    A belt-and-braces check on top of the temporal split. The split is the real
    defence; this catches the case where someone adds a convenient-looking column
    to FEATURE_COLUMNS without thinking about when it becomes knowable.
    """
    leaked = FORBIDDEN_FEATURES.intersection(features.columns)
    if leaked:
        raise ValueError(
            f"outcome-derived columns reached the feature set: {sorted(leaked)}. "
            "These are only knowable after the event being predicted."
        )


# =============================================================================
# Database wrapper
# =============================================================================


def load_frames(cutoff: date = FEATURE_CUTOFF) -> dict[str, pd.DataFrame]:
    """Read the raw inputs. Deliberately unfiltered — build_features does that.

    Reading more than is needed and filtering in one place beats filtering in the
    query, because it keeps the window logic testable without a database.
    """
    frames = {
        "users": db.read_sql(
            "SELECT user_id, signup_date, city_tier, kyc_status, kyc_completed_at FROM users"
        ),
        "transactions": db.read_sql(
            "SELECT user_id, txn_date, status FROM v_clean_transactions WHERE status = 'success'"
        ),
        "streaks": db.read_sql(
            "SELECT user_id, streak_end, streak_length, is_censored FROM user_streaks"
        ),
        # Only snapshots up to the cutoff are ever needed, and the full table is
        # 1.8M rows — this filter is a performance measure, and build_features
        # re-applies the cutoff regardless so correctness never depends on it.
        "portfolios": db.read_sql(
            "SELECT user_id, snapshot_date, portfolio_value FROM portfolio_snapshots "
            "WHERE snapshot_date <= :cutoff",
            {"cutoff": cutoff},
        ),
        "events": db.read_sql("SELECT user_id, event_date, was_eligible FROM sim_liquidity_events"),
    }
    for name, df in frames.items():
        if df.empty:
            raise RuntimeError(f"{name} is empty — run the simulation (make run-sim) first")
    return frames


def build_modelling_frame(cutoff: date = FEATURE_CUTOFF) -> tuple[pd.DataFrame, pd.Series]:
    """Load, build, validate. The single entry point the model should call."""
    frames = load_frames(cutoff)
    features = build_features(
        frames["users"],
        frames["transactions"],
        frames["streaks"],
        frames["portfolios"],
        cutoff,
    )
    assert_no_leakage(features)
    target = build_target(frames["events"], features.index, cutoff)
    return features, target


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    features, target = build_modelling_frame()
    logger.info(
        "feature summary:\n%s",
        features.describe().T[["mean", "std", "min", "50%", "max"]].round(3).to_string(),
    )
    logger.info(
        "mean feature value by class:\n%s",
        features.groupby(target.to_numpy()).mean().T.round(3).to_string(),
    )


if __name__ == "__main__":
    main()
