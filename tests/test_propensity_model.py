"""Tests for the LAMF propensity model.

The deliverable here is the *coefficients*, not the score, so these tests guard
interpretability as carefully as correctness: that odds ratios are computed from
standardised features, that the collinearity check actually fires, and that the
scaler never sees the test set.

Synthetic frames throughout, no database.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.modeling import propensity_model as pm


def synthetic(n: int = 800, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """A frame with one strong signal, one weak one, and one pure noise column."""
    rng = np.random.default_rng(seed)
    strong = rng.normal(size=n)
    weak = rng.normal(size=n)
    noise = rng.normal(size=n)
    logit = -1.6 + 1.4 * strong + 0.3 * weak
    y = rng.random(n) < 1 / (1 + np.exp(-logit))
    x = pd.DataFrame({"strong": strong, "weak": weak, "noise": noise})
    return x, pd.Series(y.astype(int), name="target")


# --------------------------------------------------------------------------- #
# Coefficients — the actual product
# --------------------------------------------------------------------------- #


def test_odds_ratios_recover_the_planted_ordering() -> None:
    x, y = synthetic()
    pipeline = pm.fit_model(x, y)
    table = pm.odds_ratios(pipeline, list(x.columns))

    assert table.index[0] == "strong"
    assert table.loc["strong", "odds_ratio"] > table.loc["weak", "odds_ratio"]
    assert table.loc["strong", "direction"] == "increases"


def test_odds_ratio_of_one_means_no_effect() -> None:
    """A pure-noise feature must land near 1.0, the null value for a ratio."""
    x, y = synthetic(n=3000)
    pipeline = pm.fit_model(x, y)
    table = pm.odds_ratios(pipeline, list(x.columns))

    assert table.loc["noise", "odds_ratio"] == pytest.approx(1.0, abs=0.15)


def test_odds_ratios_are_scale_invariant() -> None:
    """The reason features are standardised before the coefficients are read.

    Multiplying a feature by 1,000 — rupees to paise, say — must not change its
    reported effect. Without standardisation the portfolio coefficient would look
    negligible purely because of its unit, and a founder comparing it against a
    coefficient measured in days would draw the wrong conclusion.
    """
    x, y = synthetic()
    baseline = pm.odds_ratios(pm.fit_model(x, y), list(x.columns))

    rescaled = x.copy()
    rescaled["strong"] = rescaled["strong"] * 1_000
    after = pm.odds_ratios(pm.fit_model(rescaled, y), list(x.columns))

    assert after.loc["strong", "odds_ratio"] == pytest.approx(
        baseline.loc["strong", "odds_ratio"], rel=0.02
    )


def test_direction_column_matches_the_ratio() -> None:
    x, y = synthetic()
    table = pm.odds_ratios(pm.fit_model(x, y), list(x.columns))

    increases = table["odds_ratio"] >= 1
    assert (table.loc[increases, "direction"] == "increases").all()
    assert (table.loc[~increases, "direction"] == "decreases").all()


# --------------------------------------------------------------------------- #
# Collinearity
# --------------------------------------------------------------------------- #


def test_collinearity_report_flags_a_duplicated_feature() -> None:
    """The check that caught the real problem: two features measuring one thing.

    In the real feature set, successful_days was active_day_ratio x tenure_days
    and nearly deterministic of portfolio_value, producing VIF over 70 and a sign
    flip between two features that measure the same behaviour.
    """
    x, _ = synthetic()
    x["strong_copy"] = x["strong"] * 1.001 + 1e-6

    report = pm.collinearity_report(x)

    assert report.loc["strong", "vif"] > pm.VIF_THRESHOLD
    assert not report.loc["strong_copy", "safe_to_interpret"]


def test_collinearity_report_passes_independent_features() -> None:
    """A guard that flags everything is as useless as one that flags nothing."""
    x, _ = synthetic()
    report = pm.collinearity_report(x)

    assert report["safe_to_interpret"].all()


def test_the_collinear_feature_is_excluded_from_the_fit() -> None:
    """successful_days is computed for diagnostics but must not reach the model."""
    from src.modeling.feature_engineering import FEATURE_COLUMNS

    fitted = pm.model_features(FEATURE_COLUMNS)

    assert "successful_days" in FEATURE_COLUMNS
    assert "successful_days" not in fitted
    assert set(fitted) < set(FEATURE_COLUMNS)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def test_scaler_is_fitted_inside_the_pipeline() -> None:
    """Standardising before the split would leak test statistics into training.

    A small leak, but it flatters exactly the metric being reported, so the
    scaler has to live inside the pipeline rather than be applied beforehand.
    """
    x, y = synthetic()
    pipeline = pm.fit_model(x, y)

    assert "scale" in pipeline.named_steps
    assert pipeline.named_steps["scale"].mean_ is not None


def test_campaign_precision_beats_random_on_a_ranked_score() -> None:
    y = pd.Series([1] * 50 + [0] * 450)
    perfect = np.concatenate([np.linspace(1.0, 0.9, 50), np.linspace(0.4, 0.0, 450)])
    table = pm.campaign_precision(y, perfect, [50, 100])

    assert table.loc[0, "precision"] == 1.0
    assert table.loc[0, "lift_vs_random"] > 1


def test_campaign_precision_of_a_random_score_is_the_base_rate() -> None:
    """Lift near 1.0 is what "no better than random" must look like."""
    rng = np.random.default_rng(4)
    y = pd.Series(rng.random(4000) < 0.11, dtype=int)
    table = pm.campaign_precision(y, rng.random(4000), [500])

    assert table.loc[0, "lift_vs_random"] == pytest.approx(1.0, abs=0.35)


def test_campaign_sizes_beyond_the_test_set_are_skipped() -> None:
    y = pd.Series([1, 0, 1, 0])
    table = pm.campaign_precision(y, np.array([0.9, 0.1, 0.8, 0.2]), [2, 10_000])

    assert list(table["campaign_size"]) == [2]


def test_accuracy_is_labelled_as_not_a_headline() -> None:
    """At an 11% base rate, accuracy is the one metric that can look good while
    the model is worthless. The key name is the guardrail."""
    x, y = synthetic()
    pipeline = pm.fit_model(x, y)
    metrics, _ = pm.evaluate(pipeline, x, y)

    assert "accuracy_not_a_headline" in metrics
    assert "roc_auc" in metrics
    assert "average_precision" in metrics


def test_evaluate_returns_scores_in_probability_range() -> None:
    x, y = synthetic()
    pipeline = pm.fit_model(x, y)
    _, scores = pm.evaluate(pipeline, x, y)

    assert scores.min() >= 0.0
    assert scores.max() <= 1.0
