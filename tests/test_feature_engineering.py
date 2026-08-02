"""Tests for the propensity feature set — overwhelmingly about leakage.

A leaky propensity model does not fail loudly. It reports an excellent AUC, gets
shipped, and then performs at chance against real users — by which point it has
already been used to decide who to lend to. So most of what follows checks that
features cannot see the future, and the strongest test here is
``test_features_are_invariant_to_everything_after_the_cutoff``: it asserts the
property directly rather than auditing a column list by eye.

Hand-checked fixtures, no database.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.modeling import feature_engineering as fe

CUTOFF = date(2025, 6, 30)


def users_frame(rows: list[tuple] | None = None) -> pd.DataFrame:
    rows = rows or [(1, "2025-01-01", "tier_1", "verified", "2025-01-03")]
    return pd.DataFrame(
        rows, columns=["user_id", "signup_date", "city_tier", "kyc_status", "kyc_completed_at"]
    )


def txn_frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["user_id", "txn_date", "status"])


def streak_frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["user_id", "streak_end", "streak_length", "is_censored"])


def portfolio_frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["user_id", "snapshot_date", "portfolio_value"])


def _baseline() -> dict[str, pd.DataFrame]:
    """One user, investing steadily in the first half of the year."""
    return {
        "users": users_frame(),
        "transactions": txn_frame(
            [
                (1, "2025-01-01", "success"),
                (1, "2025-01-02", "success"),
                (1, "2025-06-01", "success"),
            ]
        ),
        "streaks": streak_frame([(1, "2025-01-02", 2, False)]),
        "portfolios": portfolio_frame([(1, "2025-06-30", 5_000.0)]),
    }


# --------------------------------------------------------------------------- #
# Leakage — the point of the module
# --------------------------------------------------------------------------- #


def test_features_are_invariant_to_everything_after_the_cutoff() -> None:
    """The central guarantee, asserted as a property rather than a column audit.

    Build the features, then append a mountain of post-cutoff activity — more
    transactions, longer streaks, a much larger portfolio — and rebuild. Nothing
    may move. If any feature shifts, it is reading the future, whatever its name
    suggests.
    """
    base = _baseline()
    before = fe.build_features(**base, cutoff=CUTOFF)

    polluted = {
        "users": base["users"],
        "transactions": pd.concat(
            [
                base["transactions"],
                txn_frame([(1, f"2025-07-{day:02d}", "success") for day in range(1, 29)]),
            ],
            ignore_index=True,
        ),
        "streaks": pd.concat(
            [base["streaks"], streak_frame([(1, "2025-08-01", 60, False)])], ignore_index=True
        ),
        "portfolios": pd.concat(
            [base["portfolios"], portfolio_frame([(1, "2025-12-31", 900_000.0)])],
            ignore_index=True,
        ),
    }
    after = fe.build_features(**polluted, cutoff=CUTOFF)

    pd.testing.assert_frame_equal(before, after)


def test_a_streak_still_running_at_the_cutoff_is_excluded() -> None:
    """A live streak keeps growing after the cutoff, so its length is not yet knowable.

    Counting it would read post-cutoff behaviour into a pre-cutoff feature — the
    subtle version of leakage, since the column name looks perfectly innocent.
    """
    base = _baseline()
    base["streaks"] = streak_frame([(1, "2025-07-15", 90, True)])
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert features.loc[1, "longest_streak"] == 0


def test_kyc_completed_after_the_cutoff_counts_as_incomplete() -> None:
    """At prediction time we would not know the verification was coming."""
    base = _baseline()
    base["users"] = users_frame([(1, "2025-01-01", "tier_1", "pending", "2025-09-01")])
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert features.loc[1, "kyc_incomplete"] == 1


def test_portfolio_value_is_taken_as_of_the_cutoff_not_the_latest() -> None:
    base = _baseline()
    base["portfolios"] = portfolio_frame([(1, "2025-06-30", 5_000.0), (1, "2025-12-31", 80_000.0)])
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert features.loc[1, "portfolio_value"] == pytest.approx(5_000.0)


def test_no_forbidden_column_reaches_the_feature_matrix() -> None:
    features = fe.build_features(**_baseline(), cutoff=CUTOFF)

    fe.assert_no_leakage(features)
    assert not fe.FORBIDDEN_FEATURES.intersection(features.columns)


def test_assert_no_leakage_actually_fires() -> None:
    """A guard that never triggers is not a guard."""
    features = fe.build_features(**_baseline(), cutoff=CUTOFF)
    features["resulted_in_loan"] = 1

    with pytest.raises(ValueError, match="resulted_in_loan"):
        fe.assert_no_leakage(features)


# --------------------------------------------------------------------------- #
# Target construction
# --------------------------------------------------------------------------- #


def events_frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["user_id", "event_date", "was_eligible"])


def test_only_eligible_post_cutoff_events_are_positive() -> None:
    events = events_frame(
        [
            (1, "2025-09-01", True),  # eligible, after  -> positive
            (2, "2025-09-01", False),  # ineligible, after -> negative
            (3, "2025-03-01", True),  # eligible, before  -> negative
        ]
    )
    target = fe.build_target(events, pd.Index([1, 2, 3, 4], name="user_id"), CUTOFF)

    assert target.loc[1] == 1
    assert target.loc[2] == 0
    assert target.loc[3] == 0
    assert target.loc[4] == 0  # no event at all


def test_an_event_on_the_cutoff_day_is_not_counted() -> None:
    """Strictly after. Features are stated "as of" the cutoff, so a same-day event
    is ambiguous, and one day of overlap is exactly how a score quietly inflates."""
    events = events_frame([(1, "2025-06-30", True)])
    target = fe.build_target(events, pd.Index([1], name="user_id"), CUTOFF)

    assert target.loc[1] == 0


def test_target_aligns_with_the_feature_index() -> None:
    """A misaligned target silently trains the model on shuffled labels."""
    features = fe.build_features(**_baseline(), cutoff=CUTOFF)
    target = fe.build_target(events_frame([(1, "2025-09-01", True)]), features.index, CUTOFF)

    assert target.index.equals(features.index)


# --------------------------------------------------------------------------- #
# Feature correctness
# --------------------------------------------------------------------------- #


def test_active_day_ratio_never_exceeds_one() -> None:
    """It is a share of days, so >1 is meaningless.

    The tenure count is inclusive of both endpoints: a user who signed up
    yesterday and invested on both days is active on 2 of 2 days, not 2 of 1.
    """
    base = _baseline()
    base["users"] = users_frame([(1, "2025-06-29", "tier_1", "verified", "2025-06-29")])
    base["transactions"] = txn_frame([(1, "2025-06-29", "success"), (1, "2025-06-30", "success")])
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert features.loc[1, "active_day_ratio"] == pytest.approx(1.0)
    assert features["active_day_ratio"].max() <= 1.0


def test_only_successful_transactions_count_as_activity() -> None:
    """A failed mandate is the product's fault, not evidence of a habit."""
    base = _baseline()
    base["transactions"] = txn_frame(
        [(1, "2025-01-01", "success"), (1, "2025-01-02", "failed"), (1, "2025-01-03", "skipped")]
    )
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert features.loc[1, "successful_days"] == 1


def test_users_with_no_activity_before_the_cutoff_are_excluded() -> None:
    """ "Active SIP users" is the population; someone who never invested is not one."""
    base = _baseline()
    base["users"] = users_frame(
        [
            (1, "2025-01-01", "tier_1", "verified", "2025-01-03"),
            (2, "2025-01-01", "tier_1", "verified", "2025-01-03"),
        ]
    )
    base["transactions"] = txn_frame([(1, "2025-01-01", "success"), (2, "2025-08-01", "success")])
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert list(features.index) == [1]


def test_city_tier_one_hot_uses_tier_1_as_reference() -> None:
    base = _baseline()
    base["users"] = users_frame(
        [
            (1, "2025-01-01", "tier_1", "verified", "2025-01-03"),
            (2, "2025-01-01", "tier_2", "verified", "2025-01-03"),
            (3, "2025-01-01", "tier_3", "verified", "2025-01-03"),
        ]
    )
    base["transactions"] = txn_frame([(uid, "2025-01-01", "success") for uid in (1, 2, 3)])
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert features.loc[1, ["city_tier_tier_2", "city_tier_tier_3"]].tolist() == [0.0, 0.0]
    assert features.loc[2, "city_tier_tier_2"] == 1.0
    assert features.loc[3, "city_tier_tier_3"] == 1.0


def test_missing_portfolio_defaults_to_zero_not_nan() -> None:
    """A NaN would silently drop the row at fit time; zero collateral is the truth."""
    base = _baseline()
    base["portfolios"] = portfolio_frame([(999, "2025-06-30", 100.0)])
    features = fe.build_features(**base, cutoff=CUTOFF)

    assert features.loc[1, "portfolio_value"] == 0.0
    assert not features.isna().any().any()


def test_feature_columns_are_exactly_the_declared_set() -> None:
    """Stops a stray column reaching the model without passing the leakage review."""
    features = fe.build_features(**_baseline(), cutoff=CUTOFF)

    assert list(features.columns) == fe.FEATURE_COLUMNS


def test_empty_population_raises_rather_than_returning_nothing() -> None:
    base = _baseline()
    base["transactions"] = txn_frame([(1, "2025-12-01", "success")])

    with pytest.raises(ValueError, match="no users were active"):
        fe.build_features(**base, cutoff=CUTOFF)
