"""Tests for the portfolio growth and liquidity event simulation.

Hand-checked fixtures, no database. The simulation is the foundation every later
phase stands on, so these tests mostly guard *structural* properties — that
borrowing preserves the SIP, that arm assignment is independent of eligibility,
that the funnel records its failures and not only its successes. A simulation
that quietly violates one of those would still produce plausible-looking numbers
and would invalidate the causal reading of phase 6 without anyone noticing.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.simulate import generate_liquidity_events as sim


@pytest.fixture
def config() -> sim.SimulationConfig:
    return sim.SimulationConfig(
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 10), random_seed=42
    )


def contributions_frame(rows: dict[int, list[float]], n_days: int = 10) -> pd.DataFrame:
    """Build a users x days contribution matrix from {user_id: [daily amounts]}."""
    calendar = pd.date_range("2025-01-01", periods=n_days, freq="D")
    return pd.DataFrame.from_dict(rows, orient="index", columns=calendar)


# --------------------------------------------------------------------------- #
# Portfolio growth
# --------------------------------------------------------------------------- #


def test_no_contributions_means_no_portfolio(config: sim.SimulationConfig) -> None:
    """Nothing in, nothing out — growth must not manufacture value from zero."""
    contributions = contributions_frame({1: [0.0] * 10})
    grown = sim.grow_portfolios(contributions, config, np.random.default_rng(0))

    assert (grown.to_numpy() == 0).all()


def test_contributions_accumulate(config: sim.SimulationConfig) -> None:
    contributions = contributions_frame({1: [100.0] * 10})
    grown = sim.grow_portfolios(contributions, config, np.random.default_rng(0))

    # Monotone increasing: contributions dominate any single-day market move.
    values = grown.iloc[0].to_numpy()
    assert (np.diff(values) > 0).all()
    # Ten contributions of 100 plus compounding must exceed the raw 1,000 in.
    assert values[-1] > 1_000


def test_a_contribution_earns_no_return_on_its_own_day(
    config: sim.SimulationConfig,
) -> None:
    """The conservative convention: contribute at end of day, so day 1 is exact.

    If the day's contribution were added before applying the market factor, the
    first day's balance would differ from the amount paid in — a free day of
    return the user never actually earned.
    """
    contributions = contributions_frame({1: [500.0] + [0.0] * 9})
    grown = sim.grow_portfolios(contributions, config, np.random.default_rng(0))

    assert grown.iloc[0, 0] == pytest.approx(500.0)


def test_consistent_investor_ends_up_with_more_collateral(
    config: sim.SimulationConfig,
) -> None:
    """The mechanism linking Cadence to Bridge.

    Investing consistency is not merely a model feature bolted on later — it
    determines how much collateral a user has, and therefore how much they can
    borrow. Same total contributed, different consistency, and the steady
    investor must still come out ahead because their money was in the market
    longer.
    """
    steady = [100.0] * 10
    lumpy = [0.0] * 9 + [1_000.0]
    contributions = contributions_frame({1: steady, 2: lumpy})
    grown = sim.grow_portfolios(contributions, config, np.random.default_rng(0))

    assert grown.loc[1].iloc[-1] > grown.loc[2].iloc[-1]


def test_market_shocks_are_shared_across_users(config: sim.SimulationConfig) -> None:
    """Collateral must fall together, or aggregate risk is diversified away.

    Every portfolio holds broadly the same funds, so a bad day is bad for
    everyone. Independent per-user noise would make the book look far more stable
    than it is, and the risk section would be modelling a portfolio that cannot
    exist.
    """
    contributions = contributions_frame({uid: [100.0] * 10 for uid in range(1, 6)})
    grown = sim.grow_portfolios(contributions, config, np.random.default_rng(1))

    # Identical contribution streams under a shared shock must stay identical.
    assert grown.nunique(axis=0).eq(1).all()


# --------------------------------------------------------------------------- #
# Eligibility and the treatment branch
# --------------------------------------------------------------------------- #


def _events(n: int = 400, portfolio: float = 50_000.0, tenure: int = 365) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": np.arange(1, n + 1),
            "event_date": pd.Timestamp("2025-06-01"),
            "portfolio_value": portfolio,
            "needed_amount": portfolio * 0.3,
            "days_since_signup": tenure,
            "tenure_band": "veteran",
        }
    )


def _users(n: int = 400, kyc: str = "verified") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": np.arange(1, n + 1),
            "kyc_status": kyc,
            "kyc_completed_at": pd.Timestamp("2025-01-05"),
        }
    )


def test_borrowing_never_breaks_the_sip() -> None:
    """The entire product mechanism. If this fails, nothing downstream means anything."""
    resolved = sim.resolve_events(_events(), _users(), np.random.default_rng(3))
    borrowed = resolved[resolved["resolution"] == "borrowed"]

    assert len(borrowed) > 0
    assert not borrowed["sip_broken"].any()


def test_control_arm_never_borrows() -> None:
    """No nudge, no loan — the control arm must be a clean counterfactual."""
    resolved = sim.resolve_events(_events(), _users(), np.random.default_rng(3))
    control = resolved[resolved["arm"] == "control"]

    assert (control["resolution"] == "withdrew").all()


def test_ineligible_users_cannot_borrow_even_when_nudged() -> None:
    """A nudge cannot conjure collateral that is not there."""
    thin = _events(portfolio=sim.MIN_PORTFOLIO_FOR_LAMF - 1)
    resolved = sim.resolve_events(thin, _users(), np.random.default_rng(3))

    assert not resolved["was_eligible"].any()
    assert (resolved["resolution"] == "withdrew").all()


@pytest.mark.parametrize(
    ("kwargs", "users_kwargs"),
    [
        ({"portfolio": sim.MIN_PORTFOLIO_FOR_LAMF - 1}, {}),
        ({"tenure": sim.MIN_TENURE_DAYS_FOR_LAMF - 1}, {}),
        ({}, {"kyc": "pending"}),
    ],
    ids=["thin_collateral", "too_new", "kyc_unverified"],
)
def test_every_eligibility_screen_is_binding(kwargs: dict, users_kwargs: dict) -> None:
    """All three screens must independently be able to disqualify."""
    resolved = sim.resolve_events(
        _events(**kwargs), _users(**users_kwargs), np.random.default_rng(3)
    )

    assert not resolved["was_eligible"].any()


def test_arm_assignment_is_independent_of_eligibility() -> None:
    """What licenses the causal reading in phase 6.

    Arms are assigned at the event by a coin flip. If assignment correlated with
    anything predicting stickiness, the phase 6 comparison would be measuring
    selection rather than the nudge.
    """
    mixed = pd.concat(
        [_events(n=500), _events(n=500, portfolio=100.0).assign(user_id=range(501, 1001))],
        ignore_index=True,
    )
    users = _users(n=1000)
    resolved = sim.resolve_events(mixed, users, np.random.default_rng(11))

    share_eligible_in_treatment = resolved.loc[
        resolved["arm"] == "treatment", "was_eligible"
    ].mean()
    share_eligible_in_control = resolved.loc[resolved["arm"] == "control", "was_eligible"].mean()

    assert share_eligible_in_treatment == pytest.approx(share_eligible_in_control, abs=0.06)


def test_uptake_rate_is_recovered_among_the_eligible() -> None:
    """A pipeline check, not a finding: what goes in must come back out."""
    resolved = sim.resolve_events(_events(n=4000), _users(n=4000), np.random.default_rng(5))
    offered = resolved[(resolved["arm"] == "treatment") & resolved["was_eligible"]]
    uptake = (offered["resolution"] == "borrowed").mean()

    assert uptake == pytest.approx(sim.NUDGE_UPTAKE_RATE, abs=0.04)


def test_treatment_reduces_sip_breakage_among_the_eligible() -> None:
    """The effect phase 6 is built to measure must actually be present."""
    resolved = sim.resolve_events(_events(n=4000), _users(n=4000), np.random.default_rng(5))
    eligible = resolved[resolved["was_eligible"]]
    breakage = eligible.groupby("arm")["sip_broken"].mean()

    assert breakage["treatment"] < breakage["control"]


# --------------------------------------------------------------------------- #
# Table construction
# --------------------------------------------------------------------------- #


def test_applications_record_failures_not_only_conversions() -> None:
    """A funnel that logs only its successes cannot show a drop-off.

    Every eligible, nudged event becomes an application whether or not it
    converted; only the converted ones become loans. Without that, the pipeline
    dashboard's disbursal rate would be 100% by construction.
    """
    resolved = sim.resolve_events(_events(n=2000), _users(n=2000), np.random.default_rng(7))
    tables = sim.build_tables(resolved, np.random.default_rng(7))

    eligible_treatment = int(((resolved["arm"] == "treatment") & resolved["was_eligible"]).sum())
    assert len(tables["loan_applications"]) == eligible_treatment
    assert len(tables["loans"]) < len(tables["loan_applications"])


def test_loans_never_exceed_the_max_ltv() -> None:
    """80% is a product cap, so no disbursal may sit above it."""
    resolved = sim.resolve_events(
        _events(n=2000).assign(needed_amount=lambda d: d["portfolio_value"] * 0.95),
        _users(n=2000),
        np.random.default_rng(9),
    )
    tables = sim.build_tables(resolved, np.random.default_rng(9))
    loans = tables["loans"].merge(
        tables["loan_applications"][["application_id", "user_id"]], on="application_id"
    )
    ltv = loans["disbursed_amount"] / _events(n=2000)["portfolio_value"].iloc[0]

    assert (ltv <= sim.MAX_LTV + 1e-9).all()


def test_every_loan_traces_to_an_application() -> None:
    """Mirrors the UNIQUE foreign key in the schema — no orphan disbursals."""
    resolved = sim.resolve_events(_events(n=1000), _users(n=1000), np.random.default_rng(13))
    tables = sim.build_tables(resolved, np.random.default_rng(13))

    application_ids = set(tables["loan_applications"]["application_id"])
    assert set(tables["loans"]["application_id"]) <= application_ids
    assert tables["loans"]["application_id"].is_unique


def test_withdrawal_rows_match_non_borrowing_events() -> None:
    resolved = sim.resolve_events(_events(n=1000), _users(n=1000), np.random.default_rng(13))
    tables = sim.build_tables(resolved, np.random.default_rng(13))

    assert len(tables["withdrawal_events"]) == int((resolved["resolution"] == "withdrew").sum())


def test_nudges_are_logged_for_ineligible_treatment_users_too() -> None:
    """A nudge shown to someone who cannot act on it is still a nudge sent.

    Dropping those rows would make the nudge look better than it is by hiding
    every impression that had no chance of converting.
    """
    thin = _events(n=500, portfolio=sim.MIN_PORTFOLIO_FOR_LAMF - 1)
    resolved = sim.resolve_events(thin, _users(n=500), np.random.default_rng(17))
    tables = sim.build_tables(resolved, np.random.default_rng(17))

    treatment_events = int((resolved["arm"] == "treatment").sum())
    assert len(tables["borrow_nudges"]) == treatment_events
    assert not tables["borrow_nudges"]["resulted_in_loan"].any()


# --------------------------------------------------------------------------- #
# The collateral constraint
# --------------------------------------------------------------------------- #


def test_collateral_profile_is_monotone_in_the_threshold() -> None:
    portfolios = pd.DataFrame(
        {pd.Timestamp("2025-01-01"): [100.0, 1_500.0, 3_000.0, 30_000.0]},
        index=[1, 2, 3, 4],
    )
    profile = sim.collateral_profile(portfolios)

    assert profile["users_eligible"].is_monotonic_decreasing
    assert profile.loc[profile["min_portfolio"] == 500, "users_eligible"].iat[0] == 3


def test_market_standard_ticket_is_reported_for_contrast() -> None:
    """The headline constraint must stay visible in the output, not just a comment.

    The chosen threshold is ~12x below what the LAMF market actually requires,
    and the profile table is where a reader sees how much of the book survives
    the real minimum.
    """
    portfolios = pd.DataFrame({pd.Timestamp("2025-01-01"): [1_000.0] * 10}, index=range(10))
    profile = sim.collateral_profile(portfolios)

    assert sim.MARKET_STANDARD_MIN_TICKET in set(profile["min_portfolio"])
    assert (
        profile.loc[
            profile["min_portfolio"] == sim.MARKET_STANDARD_MIN_TICKET, "users_eligible"
        ].iat[0]
        == 0
    )
