"""Tests for the weekly pipeline report.

Hand-built WeeklyRow fixtures, no database. Mostly guards the band logic — a
threshold that never fires or that fires on noise is worse than no monitoring,
because it teaches whoever reads the log to ignore it.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.reporting import loan_pipeline_report as report


def week(
    week_start: str = "2025-06-01",
    applications: int = 50,
    eligible: int = 50,
    approved: int = 45,
    disbursed: int = 17,
    disbursed_value: float = 20_000.0,
    defaults: int = 0,
) -> report.WeeklyRow:
    return report.WeeklyRow(
        week_start=date.fromisoformat(week_start),
        applications=applications,
        eligible=eligible,
        approved=approved,
        disbursed=disbursed,
        disbursed_value=disbursed_value,
        defaults=defaults,
    )


# --------------------------------------------------------------------------- #
# Rate properties
# --------------------------------------------------------------------------- #


def test_disbursal_rate_is_disbursed_over_eligible() -> None:
    w = week(eligible=50, disbursed=17)
    assert w.disbursal_rate == pytest.approx(0.34)


def test_default_rate_is_defaults_over_disbursed() -> None:
    w = week(disbursed=20, defaults=1)
    assert w.default_rate == pytest.approx(0.05)


def test_rates_are_none_not_a_division_error_on_zero_volume() -> None:
    """A quiet week must not crash the report."""
    w = week(eligible=0, disbursed=0)
    assert w.disbursal_rate is None
    assert w.default_rate is None


# --------------------------------------------------------------------------- #
# Band checking
# --------------------------------------------------------------------------- #


def test_a_rate_inside_the_band_is_not_flagged() -> None:
    in_band = week(eligible=50, disbursed=17)  # 34%, matches the assumed base case
    assert report.check_bands([in_band]) == []


def test_a_disbursal_rate_far_below_the_band_is_flagged() -> None:
    low = week(eligible=50, disbursed=2)  # 4%, well under the 18% pessimistic floor
    flags = report.check_bands([low])

    assert len(flags) == 1
    assert flags[0].metric == "disbursal_rate"


def test_a_disbursal_rate_far_above_the_band_is_flagged() -> None:
    high = week(eligible=50, disbursed=45)  # 90%, well over the 50% optimistic ceiling
    flags = report.check_bands([high])

    assert any(f.metric == "disbursal_rate" for f in flags)


def test_a_default_rate_above_its_band_is_flagged() -> None:
    bad = week(disbursed=20, defaults=5)  # 25%, far past the 3% band ceiling
    flags = report.check_bands([bad])

    assert any(f.metric == "default_rate" for f in flags)


def test_a_quiet_week_is_reported_but_not_band_checked() -> None:
    """Below MIN_APPLICATIONS_FOR_RATE_CHECK a rate is one outcome away from
    swinging 50 points — checking it would fire on noise, not signal."""
    thin = week(applications=3, eligible=3, disbursed=3)  # 100% disbursal on 3 apps

    assert report.check_bands([thin]) == []


def test_multiple_out_of_band_weeks_all_appear() -> None:
    weeks = [
        week(week_start="2025-06-01", eligible=50, disbursed=2),
        week(week_start="2025-06-08", eligible=50, disbursed=17),  # fine
        week(week_start="2025-06-15", disbursed=20, defaults=8),
    ]
    flags = report.check_bands(weeks)

    assert len(flags) == 2
    assert {f.week_start for f in flags} == {date(2025, 6, 1), date(2025, 6, 15)}


def test_band_flag_message_names_the_metric_and_the_band() -> None:
    flag = report.BandFlag(
        week_start=date(2025, 6, 1),
        metric="disbursal_rate",
        value=0.04,
        band=(0.18, 0.50),
        severity="warning",
    )
    message = flag.message()

    assert "disbursal_rate" in message
    assert "4" in message  # the value, rendered as a percentage
    assert "18%" in message and "50%" in message


# --------------------------------------------------------------------------- #
# Reconciliation against the assumed scenarios
# --------------------------------------------------------------------------- #


def test_economics_under_actuals_uses_observed_rates_not_assumptions() -> None:
    """The point of the module: recompute from what happened, not what was assumed."""
    weeks = [week(eligible=100, disbursed=10, disbursed_value=15_000.0, defaults=0)]
    actuals = report.economics_under_actuals(weeks)

    assert actuals["actual_disbursal_rate"] == pytest.approx(0.10)
    assert actuals["actual_avg_ticket"] == pytest.approx(1_500.0)


def test_economics_under_actuals_handles_a_dead_week() -> None:
    """No disbursals must not crash the reconciliation."""
    dead = [week(eligible=20, disbursed=0, disbursed_value=0.0)]
    actuals = report.economics_under_actuals(dead)

    assert actuals["net_revenue_at_actuals"] == 0.0


def test_reconciliation_includes_an_actual_row_alongside_the_scenarios() -> None:
    weeks = [week(eligible=200, disbursed=68, disbursed_value=90_000.0)]
    actuals = report.economics_under_actuals(weeks)
    table = report.reconcile_against_scenarios(actuals, eligible_users=200)

    assert "actual" in table.index
    assert {"pessimistic", "base", "optimistic"} <= set(table.index)


# --------------------------------------------------------------------------- #
# Anchoring — the bug the real run surfaced
# --------------------------------------------------------------------------- #


def test_dataframe_conversion_preserves_row_count() -> None:
    weeks = [week(week_start="2025-06-01"), week(week_start="2025-06-08")]
    frame = report.actuals_to_dataframe(weeks)

    assert len(frame) == 2
    assert "disbursal_rate" in frame.columns
