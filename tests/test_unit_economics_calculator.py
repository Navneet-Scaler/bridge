"""Tests for the unit economics model.

Two jobs. First, verify the arithmetic against hand-computed examples — an
economics model that is quietly wrong is worse than none, because its output
looks authoritative. Second, guard the *discipline*: that every input declares
its provenance, that user-side value is never counted as company revenue, and
that only incremental SIP saves are credited to the nudge.
"""

from __future__ import annotations

import pytest

from src.economics import unit_economics_calculator as ue


def simple_scenario(**overrides) -> ue.Scenario:
    """1,000 eligible users, 10% take-up, Rs 100,000 portfolios.

    Chosen so every intermediate is checkable in your head:
      loans       = 1000 * 0.10          = 100
      avg ticket  = 100000 * 0.80 * 0.50 = 40,000
      loan book   = 100 * 40,000         = 4,000,000
    """
    params = {
        "name": "test",
        "eligible_users": 1_000,
        "disbursal_rate": 0.10,
        "avg_portfolio_value": 100_000.0,
        "ltv_utilisation": 0.50,
    }
    params.update(overrides)
    return ue.Scenario(**params)


# --------------------------------------------------------------------------- #
# Arithmetic
# --------------------------------------------------------------------------- #


def test_loan_book_is_hand_checkable() -> None:
    result = ue.compute(simple_scenario())

    assert result.loans == 100
    assert result.avg_ticket == pytest.approx(40_000.0)
    assert result.loan_book == pytest.approx(4_000_000.0)


def test_gross_revenue_is_the_origination_fee_only() -> None:
    """Not the interest. In an LSP arrangement the lender earns that."""
    result = ue.compute(simple_scenario())
    expected = 4_000_000.0 * ue.value_of("origination_fee_rate")

    assert result.gross_revenue == pytest.approx(expected)
    # 9.99% of the book would be ~Rs 400k; the fee is ~Rs 60k. Confusing the two
    # would overstate revenue by roughly seven times.
    assert result.gross_revenue < 4_000_000.0 * ue.value_of("borrower_rate")


def test_expected_loss_accounts_for_collateral_recovery() -> None:
    """Default rate alone overstates loss: the collateral is liquid and sellable."""
    result = ue.compute(simple_scenario())
    expected = 4_000_000.0 * ue.value_of("default_rate") * ue.value_of("loss_given_default")

    assert result.expected_loss == pytest.approx(expected)
    assert result.expected_loss < 4_000_000.0 * ue.value_of("default_rate")


def test_provision_is_more_prudent_than_expected_loss() -> None:
    """Expected loss is an average; a market drawdown hits the whole book at once."""
    result = ue.compute(simple_scenario())

    assert result.risk_provision > result.expected_loss
    assert result.risk_provision == pytest.approx(
        result.expected_loss * ue.value_of("risk_provision_multiple")
    )


def test_net_revenue_subtracts_every_cost() -> None:
    result = ue.compute(simple_scenario())
    expected = (
        result.gross_revenue
        - result.risk_provision
        - result.servicing_cost
        - result.acquisition_cost
    )

    assert result.net_revenue == pytest.approx(expected)


def test_costs_scale_with_loan_count_not_loan_size() -> None:
    """The fixed-per-loan structure is what makes small tickets uneconomic.

    Doubling ticket size must leave servicing and acquisition untouched. If this
    ever fails, the breakeven-ticket argument silently disappears.
    """
    small = ue.compute(simple_scenario(avg_portfolio_value=10_000.0))
    large = ue.compute(simple_scenario(avg_portfolio_value=200_000.0))

    assert small.servicing_cost == large.servicing_cost
    assert small.acquisition_cost == large.acquisition_cost
    assert large.loan_book > small.loan_book


# --------------------------------------------------------------------------- #
# Keeping the user's money separate from the company's
# --------------------------------------------------------------------------- #


def test_user_value_is_not_counted_as_company_revenue() -> None:
    """The 15%-vs-9.99% spread belongs to the user, not to BlinkMoney.

    They are reported as separate fields precisely so nobody adds them together.
    """
    result = ue.compute(simple_scenario())
    expected_user_value = 4_000_000.0 * (
        ue.value_of("assumed_market_return") - ue.value_of("borrower_rate")
    )

    assert result.user_value_created == pytest.approx(expected_user_value)
    assert result.user_value_created > result.gross_revenue
    assert result.net_revenue < result.user_value_created


def test_only_incremental_sip_saves_are_credited_to_the_nudge() -> None:
    """Some users would have kept investing anyway.

    Crediting the nudge with every non-broken SIP would inflate retained AUM
    several-fold. Only the difference between the two breakage rates counts, so
    equal rates must produce exactly zero.
    """
    neutral = ue.compute(
        simple_scenario(sip_break_rate_without_nudge=0.40, sip_break_rate_with_nudge=0.40)
    )
    assert neutral.aum_retained == pytest.approx(0.0)

    effective = ue.compute(
        simple_scenario(sip_break_rate_without_nudge=0.45, sip_break_rate_with_nudge=0.35)
    )
    # 100 loans x 10pp = 10 saves x Rs 7,665 of annual contribution.
    assert effective.aum_retained == pytest.approx(10 * 7_665.0)


def test_retained_aum_compounds_over_the_horizon() -> None:
    result = ue.compute(simple_scenario(horizon_years=5))
    growth = (1 + ue.value_of("assumed_market_return")) ** 5

    assert result.aum_retained_future_value == pytest.approx(result.aum_retained * growth)
    assert result.aum_retained_future_value > result.aum_retained


# --------------------------------------------------------------------------- #
# Breakeven — the number the memo turns on
# --------------------------------------------------------------------------- #


def test_breakeven_ticket_matches_the_hand_calculation() -> None:
    fixed = ue.value_of("servicing_cost_per_loan") + ue.value_of("acquisition_cost_per_disbursal")
    margin = ue.value_of("origination_fee_rate") - (
        ue.value_of("default_rate")
        * ue.value_of("loss_given_default")
        * ue.value_of("risk_provision_multiple")
    )

    assert ue.breakeven_ticket() == pytest.approx(fixed / margin)


def test_a_loan_at_the_breakeven_ticket_earns_about_nothing() -> None:
    """The definition, verified through the full model rather than the formula.

    Sized so the average ticket lands exactly on breakeven; net revenue per loan
    should then be ~0.
    """
    breakeven = ue.breakeven_ticket()
    portfolio = breakeven / (ue.value_of("max_ltv") * 0.50)
    result = ue.compute(simple_scenario(avg_portfolio_value=portfolio))

    assert result.net_revenue_per_loan == pytest.approx(0.0, abs=1.0)


def test_tickets_below_breakeven_lose_money_at_every_volume() -> None:
    """More loans cannot fix a per-loan loss — it just multiplies it.

    This is the whole argument against pushing disbursal rate on a thin book.
    """
    thin = simple_scenario(avg_portfolio_value=2_000.0)
    low_volume = ue.compute(simple_scenario(avg_portfolio_value=2_000.0, disbursal_rate=0.05))
    high_volume = ue.compute(simple_scenario(avg_portfolio_value=2_000.0, disbursal_rate=0.80))

    assert ue.compute(thin).net_revenue < 0
    assert high_volume.net_revenue < low_volume.net_revenue


# --------------------------------------------------------------------------- #
# Discipline around the inputs
# --------------------------------------------------------------------------- #


def test_every_input_declares_a_provenance_and_a_reason() -> None:
    """The core discipline of this repo: no anonymous business number."""
    for name, item in ue.INPUTS.items():
        assert isinstance(item.provenance, ue.Provenance), name
        assert item.note.strip(), f"{name} has no explanation"
        assert len(item.note) > 40, f"{name}'s note is too thin to be useful"


def test_revenue_drivers_are_labelled_assumed_not_researched() -> None:
    """The fee rate is the biggest revenue lever and has no public source.

    If someone ever relabels it PRODUCT or INDUSTRY, the model starts presenting
    a guess as a fact — the exact failure this repo is built to avoid.
    """
    assert ue.INPUTS["origination_fee_rate"].provenance is ue.Provenance.ASSUMED
    assert ue.INPUTS["servicing_cost_per_loan"].provenance is ue.Provenance.ASSUMED
    assert ue.INPUTS["borrower_rate"].provenance is ue.Provenance.PRODUCT
    assert ue.INPUTS["max_ltv"].provenance is ue.Provenance.PRODUCT


def test_unknown_input_raises_rather_than_defaulting() -> None:
    with pytest.raises(KeyError, match="unknown input"):
        ue.value_of("take_rate_typo")


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def test_scenario_table_orders_by_optimism() -> None:
    table = ue.scenario_table(ue.base_optimistic_pessimistic(2_000, 100_000.0))

    assert set(table.index) == {"pessimistic", "base", "optimistic"}
    assert table.loc["optimistic", "loan_book"] > table.loc["base", "loan_book"]
    assert table.loc["base", "loan_book"] > table.loc["pessimistic", "loan_book"]


def test_maturity_sweep_crosses_breakeven_as_portfolios_grow() -> None:
    """Portfolio maturity is the lever that actually changes the answer."""
    sweep = ue.portfolio_maturity_sweep(1_000)

    assert not sweep.iloc[0]["viable"]
    assert sweep.iloc[-1]["viable"]
    assert sweep["net_revenue_per_loan"].is_monotonic_increasing
