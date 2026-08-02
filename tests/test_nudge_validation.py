"""Tests for the nudge treatment/control validation.

The risk in an experiment-analysis module is a confident wrong answer, so these
tests pin the statistics against known values (a z-test on a 2x2 table must equal
the chi-square, and a no-difference table must not be significant) and guard the
reporting discipline that keeps the effect from being overstated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis import nudge_validation as nv


def events_frame(
    n_control: int = 300,
    n_treatment: int = 300,
    broke_control: int = 150,
    broke_treatment: int = 100,
    eligible: bool = True,
) -> pd.DataFrame:
    """Build a resolved-events frame with exact breakage counts."""
    rows = []
    for arm, n, broke in [
        ("control", n_control, broke_control),
        ("treatment", n_treatment, broke_treatment),
    ]:
        for i in range(n):
            rows.append(
                {
                    "arm": arm,
                    "sip_broken": i < broke,
                    "was_eligible": eligible,
                    "needed_amount": 1_000.0,
                    "tenure_band": "veteran",
                    "portfolio_band": "over_3k",
                    "city_tier": "tier_1",
                }
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# The test statistics
# --------------------------------------------------------------------------- #


def test_a_real_difference_is_detected() -> None:
    result = nv.compare_arms(events_frame(), "sip_broken", "test")

    assert result.rate_control == pytest.approx(0.50)
    assert result.rate_treatment == pytest.approx(1 / 3, abs=1e-6)
    assert result.difference < 0  # negative = breakage fell = the nudge worked
    assert result.significant


def test_identical_arms_are_not_significant() -> None:
    """The guard against a test that finds an effect in anything."""
    result = nv.compare_arms(
        events_frame(broke_control=150, broke_treatment=150), "sip_broken", "null"
    )

    assert result.difference == pytest.approx(0.0)
    assert not result.significant
    assert result.p_value > 0.9


def test_a_tiny_difference_on_small_samples_is_not_significant() -> None:
    """ "Treatment did better" is not a finding until it survives chance.

    A 4pp gap on 50 events per arm is exactly the kind of difference that looks
    real in a bar chart and evaporates under a test.
    """
    result = nv.compare_arms(
        events_frame(n_control=50, n_treatment=50, broke_control=25, broke_treatment=23),
        "sip_broken",
        "small",
    )

    assert result.difference < 0
    assert not result.significant


def test_z_test_agrees_with_chi_square() -> None:
    """Algebraically equivalent on a 2x2 table, so disagreement means a bug.

    Not new evidence — a cross-check on two independent code paths before a
    single p-value gets quoted in a memo.
    """
    events = events_frame()
    z_p = nv.compare_arms(events, "sip_broken", "test").p_value
    chi_p = nv.chi_square_check(events, "sip_broken")

    assert z_p == pytest.approx(chi_p, rel=1e-6)


def test_confidence_interval_brackets_the_point_estimate() -> None:
    result = nv.compare_arms(events_frame(), "sip_broken", "test")

    assert result.ci_low < result.difference < result.ci_high


def test_confidence_interval_stays_within_possible_values() -> None:
    """A difference of proportions cannot exceed +/-100 percentage points.

    The naive normal interval can escape that range on thin segments; the
    Newcombe method is used precisely so the reported interval never contains
    impossible values.
    """
    result = nv.compare_arms(
        events_frame(n_control=45, n_treatment=45, broke_control=44, broke_treatment=1),
        "sip_broken",
        "extreme",
    )

    assert -1.0 <= result.ci_low <= 1.0
    assert -1.0 <= result.ci_high <= 1.0


def test_significant_interval_excludes_zero() -> None:
    """Internal consistency: the p-value and the interval must tell one story."""
    result = nv.compare_arms(events_frame(), "sip_broken", "test")

    assert result.significant
    assert result.ci_high < 0


def test_empty_arm_raises_rather_than_returning_nan() -> None:
    only_control = events_frame()
    only_control = only_control[only_control["arm"] == "control"]

    with pytest.raises(ValueError, match="non-empty"):
        nv.compare_arms(only_control, "sip_broken", "broken")


# --------------------------------------------------------------------------- #
# Reporting discipline
# --------------------------------------------------------------------------- #


def test_itt_is_diluted_by_users_who_cannot_borrow() -> None:
    """Why both populations are reported.

    Ineligible treatment users cannot act on the nudge, so including them must
    shrink the measured effect. Quoting only the eligible-only number would
    overstate what shipping the nudge to everyone achieves.
    """
    eligible = events_frame(eligible=True)
    ineligible = events_frame(broke_control=150, broke_treatment=150, eligible=False)
    combined = pd.concat([eligible, ineligible], ignore_index=True)

    itt = nv.compare_arms(combined, "sip_broken", "itt")
    among_eligible = nv.compare_arms(combined[combined["was_eligible"]], "sip_broken", "eligible")

    assert abs(itt.difference) < abs(among_eligible.difference)


def test_randomisation_check_passes_on_balanced_arms() -> None:
    balance = nv.randomisation_check(events_frame())

    assert not balance["significant"].any()


def test_randomisation_check_catches_imbalanced_arms() -> None:
    """If this never fired, the causal reading would rest on nothing."""
    events = events_frame()
    events.loc[events["arm"] == "treatment", "was_eligible"] = False
    balance = nv.randomisation_check(events)

    assert balance["significant"].any()


def test_thin_segments_are_dropped_not_reported() -> None:
    """A proportion test on a handful of events is confident-looking nonsense."""
    events = events_frame(n_control=200, n_treatment=200)
    thin = events_frame(n_control=5, n_treatment=5).assign(city_tier="tier_3")
    combined = pd.concat([events, thin], ignore_index=True)

    segments = nv.segment_effects(combined)

    assert "city_tier=tier_3" not in set(segments["comparison"])
    assert "city_tier=tier_1" in set(segments["comparison"])


def test_segment_effects_report_intervals_not_just_point_estimates() -> None:
    """Point estimates alone invite over-reading a segment that spans zero."""
    segments = nv.segment_effects(events_frame())

    assert not segments.empty
    for column in ("difference_pp", "ci_low_pp", "ci_high_pp", "p_value", "significant"):
        assert column in segments.columns


def test_difference_sign_convention_is_treatment_minus_control() -> None:
    """Negative must mean the nudge helped, since every chart and the memo read it
    that way. A flipped sign would invert the conclusion while still 'working'."""
    helped = nv.compare_arms(
        events_frame(broke_control=200, broke_treatment=100), "sip_broken", "helped"
    )
    hurt = nv.compare_arms(
        events_frame(broke_control=100, broke_treatment=200), "sip_broken", "hurt"
    )

    assert helped.difference < 0
    assert hurt.difference > 0


def test_rates_are_computed_per_arm_not_pooled() -> None:
    """Unequal arm sizes must not contaminate either rate."""
    result = nv.compare_arms(
        events_frame(n_control=100, n_treatment=400, broke_control=50, broke_treatment=100),
        "sip_broken",
        "unequal",
    )

    assert result.rate_control == pytest.approx(0.50)
    assert result.rate_treatment == pytest.approx(0.25)
    assert result.n_control == 100
    assert result.n_treatment == 400


def test_two_proportion_test_matches_a_hand_computed_z() -> None:
    """Pin the statistic itself against the textbook pooled-proportion formula."""
    n1 = n2 = 500
    x1, x2 = 100, 150  # treatment, control
    pooled = (x1 + x2) / (n1 + n2)
    expected_z = ((x1 / n1) - (x2 / n2)) / np.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))

    result = nv.two_proportion_test(x2, n2, x1, n1, "hand")

    assert result.z_statistic == pytest.approx(expected_z, rel=1e-6)
