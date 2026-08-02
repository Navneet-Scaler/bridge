"""LAMF propensity model — logistic regression, chosen for explainability.

Why logistic regression and not something stronger
--------------------------------------------------
A gradient-boosted tree would almost certainly score better here. It is not used,
and that is a deliberate trade rather than a limitation.

The output of this model is not a ranked list handed to a batch job. It is an
argument made to a seven-person team about **who to talk to first**, and the
thing that makes it useful is that a founder can read a coefficient and either
agree or push back. "Users whose portfolio is one standard deviation larger are
2.1x more likely to present a LAMF opportunity" is a sentence someone can act on
and challenge. "Feature 3 has SHAP importance 0.14" is not.

Explainability is also a risk control. This score would influence who gets
offered credit, and a model whose reasoning cannot be stated in a sentence is one
nobody can audit for proxy discrimination.

Evaluation
----------
The positive class is ~11% of users, so **accuracy is not reported as a headline
at all**. A model predicting "nobody is a candidate" would score 89% accurate and
be perfectly useless. ROC-AUC and average precision are the metrics; the
precision-recall curve is the one that matters operationally, because it answers
"if we call the top 200 users, how many are real opportunities".

Coefficients are reported as **odds ratios on standardised features**, so effects
are comparable across variables measured in different units — rupees against days
against ratios. Without standardisation the portfolio coefficient would look
minuscule purely because it is denominated in rupees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from statsmodels.stats.outliers_influence import variance_inflation_factor

from src.analysis import viz
from src.modeling.feature_engineering import build_modelling_frame

logger = logging.getLogger(__name__)

TEST_SIZE = 0.30
RANDOM_STATE = 42

# Inverse regularisation strength. Left mild rather than tuned: with ten features
# and ~4,000 rows the model is not capacity-constrained, and aggressive tuning
# would trade away the stable coefficients that are the actual deliverable.
REGULARISATION_C = 1.0

# Sizes a founder might realistically action in a week. The precision at these
# depths is the number that decides whether targeting beats a blanket campaign.
CAMPAIGN_SIZES = [100, 200, 500]

# Features dropped from the fit despite being computed, and why.
#
# `successful_days` is mechanically active_day_ratio x tenure_days, and is in
# turn nearly deterministic of portfolio_value (each successful day adds ~Rs 21,
# compounded). Fitting all of them together produced VIF of 72.8 and 60.7 —
# far past the conventional threshold of 10 — and the symptom was visible in the
# output: active_day_ratio came out at 5.28x while successful_days came out at
# 0.75x, opposite signs on two features that measure the same behaviour.
#
# That is fatal for this specific model, because the coefficients ARE the
# deliverable. A founder reading "investing more days makes you a worse
# candidate" would be reading an artifact of collinearity, not a finding.
#
# Dropping it costs nothing measurable: ROC-AUC is unchanged at 0.870 and
# average precision improves slightly. Every remaining VIF falls below 10, and
# portfolio_value's odds ratio drops from 1.32x to 1.07x — revealing that most
# of its apparent effect was borrowed from its collinear twin.
EXCLUDED_FROM_FIT = ["successful_days"]

# Above this, a coefficient is not safe to interpret in isolation.
VIF_THRESHOLD = 10.0


def model_features(all_features: list[str]) -> list[str]:
    """The columns actually fitted, after collinearity exclusions."""
    return [c for c in all_features if c not in EXCLUDED_FROM_FIT]


def collinearity_report(features: pd.DataFrame) -> pd.DataFrame:
    """Variance inflation factor per feature.

    Run and logged on every fit rather than checked once during development.
    Collinearity is not a static property of the code — it is a property of the
    data, so a future re-run on a different cohort can reintroduce it silently.
    Since the coefficients are the product here, a VIF regression has to be as
    visible as a metric regression.
    """
    standardised = (features - features.mean()) / features.std(ddof=1)
    standardised = standardised.assign(const=1.0)
    scores = {
        column: float(variance_inflation_factor(standardised.to_numpy(), index))
        for index, column in enumerate(standardised.columns)
        if column != "const"
    }
    table = pd.DataFrame({"vif": pd.Series(scores)}).sort_values("vif", ascending=False)
    table["safe_to_interpret"] = table["vif"] < VIF_THRESHOLD
    return table


@dataclass
class ModelReport:
    """Everything the notebook and the memo need, without refitting."""

    pipeline: Pipeline
    odds_ratios: pd.DataFrame
    metrics: dict[str, float]
    campaign_table: pd.DataFrame
    y_test: pd.Series
    y_score: np.ndarray


def fit_model(x_train: pd.DataFrame, y_train: pd.Series) -> Pipeline:
    """Fit a standardised logistic regression.

    Standardisation is inside the pipeline rather than applied beforehand, so the
    scaler is fitted on training data only. Scaling before the split would let
    the test set's mean and variance bleed into training — a small leak, but a
    real one, and it would flatter exactly the metric being reported.

    ``class_weight='balanced'`` is used because the positive class is ~11%.
    Without it the fit is dominated by negatives and the model becomes reluctant
    to predict a positive at all, which is the wrong error for a targeting model:
    missing a real candidate costs a lending relationship, while a false positive
    costs one marketing message.
    """
    pipeline = Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "logit",
                LogisticRegression(
                    C=REGULARISATION_C,
                    class_weight="balanced",
                    max_iter=1000,
                    random_state=RANDOM_STATE,
                ),
            ),
        ]
    )
    pipeline.fit(x_train, y_train)
    return pipeline


def odds_ratios(pipeline: Pipeline, feature_names: list[str]) -> pd.DataFrame:
    """Coefficients as odds ratios on standardised features.

    Each row reads: "holding everything else fixed, a one standard deviation
    increase in this feature multiplies the odds of being a LAMF candidate by
    this much." Standardising first is what makes rupees and days comparable.

    An odds ratio of 1.0 means no effect; below 1.0 means the feature reduces the
    odds. This is the table the memo quotes, so it is sorted by effect size.
    """
    logit: LogisticRegression = pipeline.named_steps["logit"]
    coefficients = logit.coef_[0]

    table = pd.DataFrame(
        {
            "feature": feature_names,
            "coefficient": coefficients,
            "odds_ratio": np.exp(coefficients),
        }
    ).set_index("feature")
    table["direction"] = np.where(table["odds_ratio"] >= 1, "increases", "decreases")
    # How far from "no effect", so the ranking is by strength not by sign.
    table["abs_log_effect"] = table["coefficient"].abs()
    return table.sort_values("abs_log_effect", ascending=False).drop(columns="abs_log_effect")


def campaign_precision(y_true: pd.Series, y_score: np.ndarray, sizes: list[int]) -> pd.DataFrame:
    """Precision if we contacted only the top-N highest-scoring users.

    This is the operational translation of the PR curve. A founder does not act
    on average precision; they act on "we can call 200 people this week — how
    many of those are worth calling". Lift against the base rate is included
    because a precision of 30% is only impressive relative to what a random 200
    would have produced.
    """
    base_rate = float(y_true.mean())
    order = np.argsort(y_score)[::-1]
    truth = y_true.to_numpy()[order]

    rows = []
    for n in sizes:
        if n > len(truth):
            continue
        hits = int(truth[:n].sum())
        precision = hits / n
        rows.append(
            {
                "campaign_size": n,
                "candidates_found": hits,
                "precision": round(precision, 4),
                "lift_vs_random": round(precision / base_rate, 2) if base_rate else float("nan"),
                "share_of_all_candidates": round(hits / truth.sum(), 4) if truth.sum() else 0.0,
            }
        )
    return pd.DataFrame(rows)


def evaluate(
    pipeline: Pipeline, x_test: pd.DataFrame, y_test: pd.Series
) -> tuple[dict, np.ndarray]:
    """Score the held-out set.

    Accuracy is computed but deliberately reported last and labelled, because at
    an 11% base rate it is the one number that can look good while the model is
    worthless. The Brier score is included as a calibration check: a targeting
    score that ranks well but is wildly overconfident will mislead anyone who
    reads the probability as a probability.
    """
    y_score = pipeline.predict_proba(x_test)[:, 1]
    metrics = {
        "roc_auc": float(roc_auc_score(y_test, y_score)),
        "average_precision": float(average_precision_score(y_test, y_score)),
        "brier_score": float(brier_score_loss(y_test, y_score)),
        "base_rate": float(y_test.mean()),
        "accuracy_not_a_headline": float((pipeline.predict(x_test) == y_test).mean()),
    }
    return metrics, y_score


# =============================================================================
# Charts
# =============================================================================


def plot_roc(y_test: pd.Series, y_score: np.ndarray, auc: float, theme: viz.Theme) -> None:
    """ROC curve against the no-skill diagonal."""
    viz.apply_style(theme)
    fig, ax = viz.new_axes(figsize=(6.6, 5.2))

    fpr, tpr, _ = roc_curve(y_test, y_score)
    ax.plot(fpr, tpr, color=theme.series[0], zorder=4)
    # Solid, one step off the surface: this is a reference, not a threshold.
    ax.plot([0, 1], [0, 1], color=theme.deemphasis, linewidth=1, zorder=2)

    viz.marker(ax, fpr[len(fpr) // 2], tpr[len(tpr) // 2], theme.series[0], theme)
    viz.label_line_end(
        ax, fpr[len(fpr) // 2], tpr[len(tpr) // 2], f"AUC {auc:.3f}", theme, dx=10, dy=-6
    )
    ax.text(0.62, 0.10, "no-skill", transform=ax.transAxes, fontsize=9, color=theme.ink_muted)

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("False positive rate")
    ax.set_ylabel("True positive rate")
    ax.set_title("Does the score separate candidates from non-candidates?")
    viz.save(fig, "propensity_roc", theme)


def plot_precision_recall(
    y_test: pd.Series, y_score: np.ndarray, ap: float, theme: viz.Theme
) -> None:
    """PR curve — the operationally meaningful one at an 11% base rate."""
    viz.apply_style(theme)
    fig, ax = viz.new_axes(figsize=(6.6, 5.2))

    precision, recall, _ = precision_recall_curve(y_test, y_score)
    ax.plot(recall, precision, color=theme.series[0], zorder=4)

    base = float(y_test.mean())
    ax.axhline(base, color=theme.deemphasis, linewidth=1, zorder=2)
    ax.text(
        0.02,
        base + 0.02,
        f"base rate {base:.1%} — what random targeting achieves",
        fontsize=9,
        color=theme.ink_muted,
    )

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Recall (share of all candidates reached)")
    ax.set_ylabel("Precision (share of those contacted who are candidates)")
    ax.set_title(f"Precision-recall (AP {ap:.3f})")
    viz.save(fig, "propensity_precision_recall", theme)


def plot_odds_ratios(table: pd.DataFrame, theme: viz.Theme) -> None:
    """Odds ratios on a log scale, diverging around 1.0.

    Log scale because odds ratios are multiplicative: 0.5 and 2.0 are equal and
    opposite effects, and a linear axis would make the halving look half as big
    as the doubling. The axis is the honest one for the quantity.
    """
    viz.apply_style(theme)
    ordered = table.sort_values("odds_ratio")
    fig, ax = viz.new_axes(figsize=(8.4, 0.42 * len(ordered) + 1.8))

    for i, (_, row) in enumerate(ordered.iterrows()):
        increases = row["odds_ratio"] >= 1
        colour = theme.diverging_high if increases else theme.diverging_low
        ax.plot([1.0, row["odds_ratio"]], [i, i], color=colour, linewidth=2, alpha=0.55, zorder=3)
        viz.marker(ax, float(row["odds_ratio"]), i, colour, theme)
        # Label on the outside of the bar, away from the 1.0 baseline. Placing
        # every label to the right would run the sub-1.0 ones back across their
        # own connector line, which reads as a strikethrough.
        viz.label_line_end(
            ax,
            float(row["odds_ratio"]),
            i,
            f"{row['odds_ratio']:.2f}x",
            theme,
            dx=9 if increases else -9,
            ha="left" if increases else "right",
        )

    ax.axvline(1.0, color=theme.axis, linewidth=1, zorder=1)
    ax.set_xscale("log")
    # Explicit ticks: the default log locator emits a lone 10^0 here, which
    # leaves the reader no scale to judge distance against.
    ticks = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t:g}x" for t in ticks])
    ax.set_xlim(min(0.3, ordered["odds_ratio"].min() * 0.7), ordered["odds_ratio"].max() * 1.7)
    ax.minorticks_off()
    ax.set_yticks(np.arange(len(ordered)))
    ax.set_yticklabels(ordered.index, fontsize=9, color=theme.ink_secondary)
    ax.set_xlabel("Odds ratio per 1 SD (>1 = more likely to be a LAMF candidate)")
    ax.set_title("What predicts a LAMF opportunity?")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.set_axisbelow(True)
    viz.save(fig, "propensity_odds_ratios", theme)


# =============================================================================
# Entry point
# =============================================================================


def run() -> ModelReport:
    """Build features, fit, evaluate, and write figures."""
    all_features, target = build_modelling_frame()
    features = all_features[model_features(list(all_features.columns))]

    vif_table = collinearity_report(features)
    logger.info("collinearity check:\n%s", vif_table.round(2).to_string())
    unsafe = vif_table[~vif_table["safe_to_interpret"]]
    if not unsafe.empty:
        logger.warning(
            "VIF above %.0f for %s — coefficients for these are not safe to read "
            "in isolation and must not be quoted individually in the memo.",
            VIF_THRESHOLD,
            ", ".join(unsafe.index),
        )

    x_train, x_test, y_train, y_test = train_test_split(
        features,
        target,
        test_size=TEST_SIZE,
        random_state=RANDOM_STATE,
        # Stratified so both splits carry the same base rate. Without it, an
        # unlucky split changes the reported precision for reasons that have
        # nothing to do with the model.
        stratify=target,
    )
    logger.info(
        "train %s rows (%.1f%% positive), test %s rows (%.1f%% positive)",
        f"{len(x_train):,}",
        100 * y_train.mean(),
        f"{len(x_test):,}",
        100 * y_test.mean(),
    )

    pipeline = fit_model(x_train, y_train)
    metrics, y_score = evaluate(pipeline, x_test, y_test)
    ratios = odds_ratios(pipeline, list(features.columns))
    campaigns = campaign_precision(y_test, y_score, CAMPAIGN_SIZES)

    logger.info("held-out metrics: %s", {k: round(v, 4) for k, v in metrics.items()})
    logger.info("odds ratios (per 1 SD):\n%s", ratios.round(4).to_string())
    logger.info("campaign precision:\n%s", campaigns.to_string(index=False))

    for theme in viz.THEMES.values():
        plot_roc(y_test, y_score, metrics["roc_auc"], theme)
        plot_precision_recall(y_test, y_score, metrics["average_precision"], theme)
        plot_odds_ratios(ratios, theme)

    return ModelReport(
        pipeline=pipeline,
        odds_ratios=ratios,
        metrics=metrics,
        campaign_table=campaigns,
        y_test=y_test,
        y_score=y_score,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    report = run()
    top = report.odds_ratios.index[0]
    logger.info(
        "strongest predictor: %s (%.2fx per SD)",
        top,
        report.odds_ratios.loc[top, "odds_ratio"],
    )


if __name__ == "__main__":
    main()
