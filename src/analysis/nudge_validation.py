"""Does nudging users to borrow actually prevent SIP breakage?

The comparison
--------------
Every liquidity event was assigned an arm by a coin flip **at the event**,
independent of any user attribute, so the two arms are exchangeable and the
difference between them is attributable to the nudge rather than to selection.

Two populations are reported, and the distinction is the most important thing in
this module:

* **Intention-to-treat (ITT)** — every treatment event against every control
  event, including treatment users who were never eligible to borrow. This is the
  honest estimate of what shipping the nudge to everyone achieves, because in
  production you cannot only ship it to people who will convert.

* **Among the eligible** — restricted to events where the user actually cleared
  the eligibility screen. This isolates the nudge's effect on the population it
  can physically reach.

ITT is always the smaller number, and quoting only the eligible-only figure would
overstate the intervention. Both are reported for that reason. The eligible-only
comparison remains randomised, because eligibility is determined by portfolio,
tenure and KYC — all fixed before the coin flip — so conditioning on it does not
break the randomisation.

Why a z-test and not just the difference
----------------------------------------
"Treatment did better" is not a finding until it survives the question "would a
fair coin have produced this gap?". With a few hundred events per arm, a several
point difference in breakage is well within what chance produces. The two-
proportion z-test answers that directly, and the confidence interval on the
difference is reported alongside it because a p-value alone says whether an
effect exists, not how big it plausibly is — and the size is what the economics
model needs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.proportion import confint_proportions_2indep, proportions_ztest

from src import db
from src.analysis import viz

logger = logging.getLogger(__name__)

ALPHA = 0.05

# Segments the effect is broken down by. Chosen because each maps to a decision:
# who to show the nudge to first, and where it is wasted.
SEGMENT_COLUMNS = ["tenure_band", "portfolio_band", "city_tier"]

# A segment thinner than this cannot support a proportion test; reporting one
# anyway produces confident-looking nonsense on a handful of events.
MIN_SEGMENT_EVENTS = 40


@dataclass
class ProportionTest:
    """Result of one two-proportion comparison."""

    label: str
    n_control: int
    n_treatment: int
    rate_control: float
    rate_treatment: float
    difference: float
    ci_low: float
    ci_high: float
    z_statistic: float
    p_value: float
    significant: bool

    def as_row(self) -> dict:
        return {
            "comparison": self.label,
            "n_control": self.n_control,
            "n_treatment": self.n_treatment,
            "rate_control": round(self.rate_control, 4),
            "rate_treatment": round(self.rate_treatment, 4),
            "difference_pp": round(100 * self.difference, 2),
            "ci_low_pp": round(100 * self.ci_low, 2),
            "ci_high_pp": round(100 * self.ci_high, 2),
            "p_value": float(f"{self.p_value:.2e}"),
            "significant": self.significant,
        }


def two_proportion_test(
    successes_control: int,
    n_control: int,
    successes_treatment: int,
    n_treatment: int,
    label: str,
    alpha: float = ALPHA,
) -> ProportionTest:
    """Two-proportion z-test plus a confidence interval on the difference.

    The interval uses the Newcombe method rather than a naive normal
    approximation, because with small counts in thin segments the naive interval
    can extend past 0% or 100% — an interval that includes impossible values
    undermines the number it is meant to support.

    ``difference`` is treatment minus control, so a *negative* value means the
    nudge reduced the rate, which is the desired direction for breakage.
    """
    if n_control == 0 or n_treatment == 0:
        raise ValueError(f"{label}: both arms must be non-empty")

    rate_control = successes_control / n_control
    rate_treatment = successes_treatment / n_treatment

    z_statistic, p_value = proportions_ztest(
        count=np.array([successes_treatment, successes_control]),
        nobs=np.array([n_treatment, n_control]),
    )
    ci_low, ci_high = confint_proportions_2indep(
        count1=successes_treatment,
        nobs1=n_treatment,
        count2=successes_control,
        nobs2=n_control,
        method="newcomb",
        compare="diff",
        alpha=alpha,
    )

    return ProportionTest(
        label=label,
        n_control=n_control,
        n_treatment=n_treatment,
        rate_control=rate_control,
        rate_treatment=rate_treatment,
        difference=rate_treatment - rate_control,
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        z_statistic=float(z_statistic),
        p_value=float(p_value),
        significant=bool(p_value < alpha),
    )


def compare_arms(events: pd.DataFrame, outcome: str, label: str) -> ProportionTest:
    """Run the arm comparison for a boolean outcome column."""
    control = events[events["arm"] == "control"]
    treatment = events[events["arm"] == "treatment"]
    return two_proportion_test(
        successes_control=int(control[outcome].sum()),
        n_control=len(control),
        successes_treatment=int(treatment[outcome].sum()),
        n_treatment=len(treatment),
        label=label,
    )


def chi_square_check(events: pd.DataFrame, outcome: str) -> float:
    """Chi-square on the same 2x2 table, as a cross-check on the z-test.

    The two are algebraically equivalent for a 2x2 table (chi-square equals z
    squared), so agreement is not new evidence — it is a guard against a coding
    error in either path, which is worth having when a single p-value is about to
    be quoted in a memo.
    """
    table = pd.crosstab(events["arm"], events[outcome])
    _, p_value, _, _ = stats.chi2_contingency(table, correction=False)
    return float(p_value)


def randomisation_check(events: pd.DataFrame) -> pd.DataFrame:
    """Verify the arms are balanced on pre-treatment characteristics.

    A randomised assignment should produce arms that look alike on everything
    fixed before the coin flip. This is the check that earns the right to read
    the difference causally — and it is run *before* the outcome is examined, so
    it cannot be rationalised after seeing the result. Any covariate differing
    significantly here would mean the randomisation failed and the headline
    number is measuring selection.
    """
    rows = []
    for column in ["was_eligible"]:
        rows.append(compare_arms(events, column, f"balance: {column}").as_row())

    numeric = events.groupby("arm")["needed_amount"].agg(["mean", "std", "size"])
    control_mean = numeric.loc["control", "mean"]
    treatment_mean = numeric.loc["treatment", "mean"]
    t_stat, p_value = stats.ttest_ind(
        events.loc[events["arm"] == "treatment", "needed_amount"],
        events.loc[events["arm"] == "control", "needed_amount"],
        equal_var=False,
    )
    rows.append(
        {
            "comparison": "balance: needed_amount",
            "n_control": int(numeric.loc["control", "size"]),
            "n_treatment": int(numeric.loc["treatment", "size"]),
            "rate_control": round(control_mean, 2),
            "rate_treatment": round(treatment_mean, 2),
            "difference_pp": round(treatment_mean - control_mean, 2),
            "ci_low_pp": np.nan,
            "ci_high_pp": np.nan,
            "p_value": float(f"{p_value:.2e}"),
            "significant": bool(p_value < ALPHA),
        }
    )
    return pd.DataFrame(rows)


def segment_effects(events: pd.DataFrame, outcome: str = "sip_broken") -> pd.DataFrame:
    """Effect of the nudge within each segment level.

    Segmenting after seeing the headline result is a well-known way to find
    effects that are not there, so two guards apply. Levels thinner than
    MIN_SEGMENT_EVENTS are dropped rather than reported, and the summary flags
    that these are exploratory: with a dozen comparisons, roughly one will clear
    p < 0.05 by chance alone, so a lone significant segment is a hypothesis
    rather than a finding.
    """
    rows = []
    for column in SEGMENT_COLUMNS:
        if column not in events.columns:
            continue
        for level, group in events.groupby(column):
            if len(group) < MIN_SEGMENT_EVENTS:
                continue
            if group["arm"].nunique() < 2:
                continue
            result = compare_arms(group, outcome, f"{column}={level}")
            row = result.as_row()
            row["segment"] = column
            row["level"] = str(level)
            rows.append(row)

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).sort_values("difference_pp")
    return frame


def load_events() -> pd.DataFrame:
    """Read the resolved liquidity events, joined to what is needed to segment.

    ``sip_broken`` is reconstructed by joining withdrawals on (user, date): a
    borrowed event has no withdrawal row, and by construction never breaks the
    SIP, which is the mechanism under test.
    """
    events = db.read_sql(
        """
        SELECT e.user_id,
               e.event_date,
               e.needed_amount,
               e.arm,
               e.was_eligible,
               e.resolution,
               COALESCE(w.sip_broken, FALSE) AS sip_broken,
               u.city_tier,
               (e.event_date - u.signup_date) AS tenure_days,
               p.portfolio_value
        FROM sim_liquidity_events e
        JOIN users u ON u.user_id = e.user_id
        LEFT JOIN withdrawal_events w
               ON w.user_id = e.user_id AND w.event_date = e.event_date
        LEFT JOIN portfolio_snapshots p
               ON p.user_id = e.user_id AND p.snapshot_date = e.event_date
        """
    )
    if events.empty:
        raise RuntimeError("no liquidity events — run the simulation (make run-sim) first")

    events["tenure_band"] = pd.cut(
        events["tenure_days"],
        bins=[-1, 120, 240, 10_000],
        labels=["new", "established", "veteran"],
    )
    events["portfolio_band"] = pd.cut(
        events["portfolio_value"].fillna(0),
        bins=[-1, 1_000, 3_000, 1e12],
        labels=["under_1k", "1k_to_3k", "over_3k"],
    )
    return events


# =============================================================================
# Charts
# =============================================================================


def plot_breakage(itt: ProportionTest, eligible: ProportionTest, theme: viz.Theme) -> None:
    """Breakage rate by arm, for both populations.

    Grouped bars rather than a single pair, because showing only the
    eligible-only result would overstate what shipping the nudge achieves.
    """
    viz.apply_style(theme)
    fig, ax = viz.new_axes(figsize=(7.6, 4.8))

    groups = [("All events (ITT)", itt), ("Eligible only", eligible)]
    width = 0.34
    positions = np.arange(len(groups))

    for offset, (arm, colour, key) in enumerate(
        [
            ("control", theme.series[1], "rate_control"),
            ("treatment", theme.series[0], "rate_treatment"),
        ]
    ):
        values = [getattr(result, key) for _, result in groups]
        # 2px of surface between adjacent bars, not a stroke around them.
        bars = ax.bar(
            positions + (offset - 0.5) * (width + 0.012),
            values,
            width=width,
            color=colour,
            label=arm,
            zorder=3,
        )
        for bar, value in zip(bars, values, strict=False):
            ax.annotate(
                f"{value:.1%}",
                xy=(bar.get_x() + bar.get_width() / 2, value),
                xytext=(0, 5),
                textcoords="offset points",
                ha="center",
                fontsize=9.5,
                color=theme.ink_secondary,
            )

    ax.set_xticks(positions)
    ax.set_xticklabels([name for name, _ in groups])
    ax.set_ylim(0, max(itt.rate_control, eligible.rate_control) * 1.35)
    ax.set_ylabel("Share of events that broke the SIP")
    ax.set_title("Does the borrow nudge prevent SIP breakage?")
    ax.legend(loc="upper right")
    ax.grid(axis="x", visible=False)

    for i, (_, result) in enumerate(groups):
        stars = "significant" if result.significant else "not significant"
        ax.annotate(
            f"{100 * result.difference:+.1f}pp · p={result.p_value:.1e} · {stars}",
            xy=(i, 0),
            xytext=(0, -32),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color=theme.ink_muted,
        )

    viz.save(fig, "nudge_breakage_by_arm", theme)


def plot_segment_effects(segments: pd.DataFrame, theme: viz.Theme) -> None:
    """Per-segment effect with confidence intervals, on a zero baseline.

    Intervals are drawn because the point estimates are what tempt over-reading:
    a segment whose interval spans zero has not shown an effect, however large
    its point estimate looks.
    """
    if segments.empty:
        return
    viz.apply_style(theme)
    ordered = segments.sort_values("difference_pp", ascending=False).reset_index(drop=True)
    fig, ax = viz.new_axes(figsize=(8.4, 0.46 * len(ordered) + 2.0))

    for i, row in ordered.iterrows():
        # Negative difference = breakage fell = the nudge worked.
        helped = row["difference_pp"] < 0
        colour = theme.status_good if (helped and row["significant"]) else theme.deemphasis
        ax.plot(
            [row["ci_low_pp"], row["ci_high_pp"]], [i, i], color=colour, linewidth=2, alpha=0.55
        )
        viz.marker(ax, float(row["difference_pp"]), i, colour, theme)
        suffix = "" if row["significant"] else "  n.s."
        viz.label_line_end(
            ax, float(row["ci_high_pp"]), i, f"{row['difference_pp']:+.1f}pp{suffix}", theme
        )

    ax.axvline(0.0, color=theme.axis, linewidth=1, zorder=1)
    ax.set_yticks(np.arange(len(ordered)))
    ax.set_yticklabels(ordered["comparison"], fontsize=9, color=theme.ink_secondary)
    ax.set_xlabel("Change in SIP breakage, treatment vs control (pp; negative is better)")
    ax.set_title("Where does the nudge work?")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    viz.save(fig, "nudge_segment_effects", theme)


# =============================================================================
# Entry point
# =============================================================================


def run() -> dict:
    """Run the full validation and write figures."""
    events = load_events()
    logger.info(
        "loaded %s events (%s treatment, %s control)",
        f"{len(events):,}",
        f"{int((events['arm'] == 'treatment').sum()):,}",
        f"{int((events['arm'] == 'control').sum()):,}",
    )

    # Balance first, before looking at any outcome.
    balance = randomisation_check(events)
    logger.info("randomisation balance check:\n%s", balance.to_string(index=False))
    if balance["significant"].any():
        logger.warning(
            "arms differ on a pre-treatment covariate — the headline effect may be "
            "selection rather than the nudge"
        )

    itt = compare_arms(events, "sip_broken", "ITT (all events)")
    eligible_events = events[events["was_eligible"]]
    eligible = compare_arms(eligible_events, "sip_broken", "Among eligible")

    headline = pd.DataFrame([itt.as_row(), eligible.as_row()])
    logger.info("SIP breakage, treatment vs control:\n%s", headline.to_string(index=False))

    chi_p = chi_square_check(eligible_events, "sip_broken")
    logger.info(
        "chi-square cross-check on the eligible population: p=%.3e (z-test p=%.3e)",
        chi_p,
        eligible.p_value,
    )

    segments = segment_effects(eligible_events)
    if not segments.empty:
        logger.info(
            "segment effects (EXPLORATORY — %d comparisons, expect ~%.1f false "
            "positives at alpha=%.2f):\n%s",
            len(segments),
            len(segments) * ALPHA,
            ALPHA,
            segments[
                [
                    "comparison",
                    "n_control",
                    "n_treatment",
                    "difference_pp",
                    "p_value",
                    "significant",
                ]
            ].to_string(index=False),
        )

    for theme in viz.THEMES.values():
        plot_breakage(itt, eligible, theme)
        plot_segment_effects(segments, theme)

    return {"itt": itt, "eligible": eligible, "segments": segments, "balance": balance}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    results = run()
    eligible: ProportionTest = results["eligible"]
    logger.info(
        "headline: among eligible users the nudge changed SIP breakage by %+.1fpp "
        "(95%% CI %.1f to %.1f, p=%.3e)",
        100 * eligible.difference,
        100 * eligible.ci_low,
        100 * eligible.ci_high,
        eligible.p_value,
    )


if __name__ == "__main__":
    main()
