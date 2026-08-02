"""Weekly LAMF pipeline health report.

Pulls the latest week of applications, disbursals and nudge outcomes from
``v_lamf_pipeline``, recomputes unit economics under **current actuals** rather
than the assumed scenario, and flags any week where disbursal rate or default
rate has moved outside an expected band.

Actuals, not assumptions
-------------------------
``unit_economics_calculator.py`` answers "what could this look like". This
module answers "what did it actually do last week" — the two are meant to
diverge, and the gap between them is itself informative: an actual disbursal
rate running well below the assumed 34% is exactly the finding a founder needs
surfaced automatically, not discovered by opening a notebook.

Why bands and not a fixed threshold
------------------------------------
A single fixed threshold ("alert if disbursal rate < 20%") gets outgrown by a
maturing book or miscalibrated by an early one. Bands are instead centred on the
scenario range from the economics model (pessimistic-to-optimistic disbursal
rate, and the industry default-rate assumption with slack), so an alert means
"this week fell outside the range the model was built to expect", which is a
statement that stays meaningful as the book grows.

Logging, not print
-------------------
Every message goes through ``logging`` so this can run under cron with output
captured to a file, and so severity (INFO vs WARNING) is machine-parseable by
whatever forwards these logs — which matters the moment this becomes an
unattended weekly job rather than a script someone watches run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from src import db
from src.economics.unit_economics_calculator import (
    Scenario,
    base_optimistic_pessimistic,
    compute,
    value_of,
)

logger = logging.getLogger(__name__)

# Disbursal-rate band: the full spread the economics model's scenarios cover.
# A week landing outside this range means actuals have left the range the model
# was built to expect, which is the trigger for a WARNING rather than an INFO.
DISBURSAL_RATE_BAND = (0.18, 0.50)  # matches pessimistic..optimistic in the calculator

# Default-rate band: the assumed 1.2% plus slack either side. LAMF default is
# structurally low and slow-moving (collateral is liquid and marked daily), so a
# single week drifting far from the assumption is worth a look rather than a
# certainty of trouble — hence a band, not a hard ceiling.
DEFAULT_RATE_BAND = (0.0, 0.03)

# A week with fewer applications than this cannot support a rate estimate
# without excessive noise; it is reported but not band-checked.
MIN_APPLICATIONS_FOR_RATE_CHECK = 10


@dataclass
class WeeklyRow:
    """One week of the pipeline, as read from v_lamf_pipeline."""

    week_start: date
    applications: int
    eligible: int
    approved: int
    disbursed: int
    disbursed_value: float
    defaults: int

    @property
    def disbursal_rate(self) -> float | None:
        return self.disbursed / self.eligible if self.eligible else None

    @property
    def default_rate(self) -> float | None:
        return self.defaults / self.disbursed if self.disbursed else None


@dataclass
class BandFlag:
    """One out-of-band finding, in the shape the log line and the alert need."""

    week_start: date
    metric: str
    value: float
    band: tuple[float, float]
    severity: str  # "warning" — nothing here is "critical": see the note below.

    def message(self) -> str:
        low, high = self.band
        return (
            f"{self.week_start}: {self.metric} = {self.value:.2%}, outside the "
            f"expected band [{low:.0%}, {high:.0%}]"
        )


def load_weekly_pipeline(weeks: int = 8, as_of: date | None = None) -> list[WeeklyRow]:
    """Read the last N weeks from Cadence's/Bridge's shared v_lamf_pipeline view.

    Reads more than one week on purpose: a single week's numbers are meaningless
    without the trend they sit in, and the report always prints both.

    Anchored on the LATEST week actually present in the table, not on wall-clock
    "today". A weekly cron job runs against data that is current at the time it
    runs, so today() would be the right anchor in production. But it is the wrong
    anchor for a simulated book seeded with a fixed historical date range: the
    moment wall-clock time moves past the simulation's end date, a today()-anchored
    window silently finds nothing and the job goes quiet instead of reporting a
    problem — a false "all clear" is worse than the noisy failure this guards
    against. Anchoring on the data's own latest week makes the report correct in
    both cases without needing to know which one it is in.
    """
    if as_of is None:
        latest = db.read_sql("SELECT MAX(week_start) AS d FROM v_lamf_pipeline")["d"].iat[0]
        if pd.isna(latest):
            raise RuntimeError("v_lamf_pipeline is empty — run the simulation (make run-sim) first")
        as_of = pd.Timestamp(latest).date()

    since = as_of - timedelta(weeks=weeks)
    frame = db.read_sql(
        "SELECT * FROM v_lamf_pipeline WHERE week_start >= :since AND week_start <= :as_of "
        "ORDER BY week_start",
        {"since": since, "as_of": as_of},
    )
    if frame.empty:
        raise RuntimeError(
            "no rows in v_lamf_pipeline for the requested window — run the "
            "simulation (make run-sim) first"
        )
    return [
        WeeklyRow(
            week_start=row.week_start,
            applications=int(row.applications),
            eligible=int(row.eligible),
            approved=int(row.approved),
            disbursed=int(row.disbursed),
            disbursed_value=float(row.disbursed_value),
            defaults=int(row.defaults),
        )
        for row in frame.itertuples()
    ]


def check_bands(weeks: list[WeeklyRow]) -> list[BandFlag]:
    """Flag any week whose disbursal or default rate falls outside its band.

    Only weeks with enough volume are checked — see MIN_APPLICATIONS_FOR_RATE_CHECK
    — because a rate computed from three applications swings 33 points on a
    single outcome and would fire constantly on noise rather than signal.
    """
    flags: list[BandFlag] = []
    for week in weeks:
        if week.applications < MIN_APPLICATIONS_FOR_RATE_CHECK:
            continue

        if week.disbursal_rate is not None:
            low, high = DISBURSAL_RATE_BAND
            if not (low <= week.disbursal_rate <= high):
                flags.append(
                    BandFlag(
                        week.week_start,
                        "disbursal_rate",
                        week.disbursal_rate,
                        DISBURSAL_RATE_BAND,
                        "warning",
                    )
                )

        if week.default_rate is not None:
            low, high = DEFAULT_RATE_BAND
            if not (low <= week.default_rate <= high):
                flags.append(
                    BandFlag(
                        week.week_start,
                        "default_rate",
                        week.default_rate,
                        DEFAULT_RATE_BAND,
                        "warning",
                    )
                )
    return flags


def actuals_to_dataframe(weeks: list[WeeklyRow]) -> pd.DataFrame:
    """Weekly rows as a frame, for the log table and the reconciliation check."""
    return pd.DataFrame(
        [
            {
                "week_start": w.week_start,
                "applications": w.applications,
                "eligible": w.eligible,
                "approved": w.approved,
                "disbursed": w.disbursed,
                "disbursed_value": w.disbursed_value,
                "defaults": w.defaults,
                "disbursal_rate": w.disbursal_rate,
                "default_rate": w.default_rate,
            }
            for w in weeks
        ]
    )


def economics_under_actuals(weeks: list[WeeklyRow]) -> dict[str, float]:
    """Recompute unit economics from what actually happened, not the assumption.

    Rolls up the window into one scenario rather than one economics run per week
    — a single week's disbursal count is too small to price reliably, but the
    trailing window is exactly what the assumed scenario range was meant to
    bound, so this is the number to compare against it.
    """
    total_eligible = sum(w.eligible for w in weeks)
    total_disbursed = sum(w.disbursed for w in weeks)
    total_value = sum(w.disbursed_value for w in weeks)
    total_defaults = sum(w.defaults for w in weeks)

    if total_eligible == 0 or total_disbursed == 0:
        return {
            "actual_disbursal_rate": 0.0,
            "actual_default_rate": 0.0,
            "actual_avg_ticket": 0.0,
            "net_revenue_at_actuals": 0.0,
        }

    actual_disbursal_rate = total_disbursed / total_eligible
    actual_default_rate = total_defaults / total_disbursed
    actual_avg_ticket = total_value / total_disbursed
    # Back out the average portfolio value implied by the observed ticket, so the
    # calculator's LTV logic (avg_ticket = portfolio * max_ltv * utilisation) is
    # respected rather than bypassed.
    implied_portfolio = actual_avg_ticket / (value_of("max_ltv") * 0.55)

    scenario = Scenario(
        name="actuals",
        eligible_users=total_eligible,
        disbursal_rate=actual_disbursal_rate,
        avg_portfolio_value=implied_portfolio,
        default_rate=actual_default_rate,
    )
    result = compute(scenario)
    return {
        "actual_disbursal_rate": round(actual_disbursal_rate, 4),
        "actual_default_rate": round(actual_default_rate, 4),
        "actual_avg_ticket": round(actual_avg_ticket, 2),
        "net_revenue_at_actuals": result.net_revenue,
    }


def reconcile_against_scenarios(actuals: dict[str, float], eligible_users: int) -> pd.DataFrame:
    """Line up actuals next to the pessimistic/base/optimistic scenarios.

    The reconciliation this function name promises: is what is actually
    happening inside, above, or below the range the economics model was built
    to expect. A founder reading only the scenario table would not otherwise
    know which of the three cases the business is currently living in.
    """
    avg_portfolio = actuals["actual_avg_ticket"] / (value_of("max_ltv") * 0.55)
    scenarios = base_optimistic_pessimistic(eligible_users, avg_portfolio)
    table = pd.DataFrame([compute(s).__dict__ for s in scenarios]).drop(columns="details")
    table = table.set_index("scenario")[["loans", "net_revenue"]]

    actual_row = pd.DataFrame(
        [{"loans": None, "net_revenue": actuals["net_revenue_at_actuals"]}], index=["actual"]
    )
    return pd.concat([table, actual_row])


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )

    weeks = load_weekly_pipeline()
    frame = actuals_to_dataframe(weeks)
    logger.info("pipeline, last %d weeks:\n%s", len(weeks), frame.to_string(index=False))

    flags = check_bands(weeks)
    if flags:
        for flag in flags:
            logger.warning(flag.message())
    else:
        logger.info("no weeks fell outside the expected disbursal or default bands")

    total_eligible = sum(w.eligible for w in weeks)
    actuals = economics_under_actuals(weeks)
    logger.info("economics under actuals: %s", actuals)

    reconciliation = reconcile_against_scenarios(actuals, total_eligible)
    logger.info("actuals vs assumed scenarios:\n%s", reconciliation.to_string())

    logger.info(
        "summary: %d weeks reviewed, %d band flags, actual disbursal rate %.1f%% "
        "(assumed range %.0f-%.0f%%), actual default rate %.2f%% (assumed band "
        "%.1f-%.1f%%)",
        len(weeks),
        len(flags),
        100 * actuals["actual_disbursal_rate"],
        100 * DISBURSAL_RATE_BAND[0],
        100 * DISBURSAL_RATE_BAND[1],
        100 * actuals["actual_default_rate"],
        100 * DEFAULT_RATE_BAND[0],
        100 * DEFAULT_RATE_BAND[1],
    )


if __name__ == "__main__":
    main()
