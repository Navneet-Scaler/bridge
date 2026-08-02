"""LAMF unit economics — revenue, risk, and retained AUM under stated assumptions.

Read this before quoting any number out of this file
----------------------------------------------------
BlinkMoney's real take rate, funding arrangement and loss experience are not
public. **Nothing in here is a researched fact about their business.** Every
input is one of three things, and each is labelled as such in ``INPUTS`` below:

* ``PRODUCT``   — advertised by the product itself (the 9.99% rate, 80% LTV).
* ``INDUSTRY``  — a structural feature of how LAMF lending works in India,
                  reasoned from the product's mechanics rather than measured.
* ``ASSUMED``   — a number chosen for modelling. No source. Argue with it.

The distinction is the point. A model that presents an assumed 1.5% origination
fee as though it were BlinkMoney's actual take rate is worse than no model,
because it produces a confident revenue figure a founder might plan against.
Everything here is built so the assumptions are visible, named, and changeable in
one place — and so the scenario table shows how much the answer moves when they
are wrong.

Whose revenue is whose
----------------------
Two spreads are easy to conflate, and conflating them overstates the business by
an order of magnitude:

* The **user's** spread is ~15% earned minus 9.99% paid. That is value delivered
  to the *user*, and it is the product's pitch. **It is not BlinkMoney revenue.**
* **BlinkMoney's** revenue, in a Lending Service Provider arrangement, is a
  sourcing or origination fee on the disbursed amount. The lender's balance
  sheet carries the loan and earns the interest.

``user_value_created`` and ``gross_revenue`` are kept as separate outputs for
exactly this reason.

The binding constraint
----------------------
Phase 2 established that this cohort cannot support LAMF at market-standard
minimum tickets: a one-year Rs 21/day micro-SIP book has a median portfolio under
Rs 900 and no user above Rs 11,000, against a typical Rs 25,000 minimum. Every
revenue figure this module produces is therefore an **upper bound on a book that
could not be written today at standard economics**. The scenario table treats
portfolio maturity as a first-class axis for that reason, rather than sweeping
disbursal rate alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

logger = logging.getLogger(__name__)


class Provenance(str, Enum):
    """Where a number came from. Printed alongside every input."""

    PRODUCT = "PRODUCT"  # advertised by the product
    INDUSTRY = "INDUSTRY"  # structural to how LAMF works; reasoned, not measured
    ASSUMED = "ASSUMED"  # chosen for modelling; no source


# =============================================================================
# INPUTS — every business number, named, with its provenance and reasoning
# =============================================================================


@dataclass(frozen=True)
class Input:
    """One business input: its value, where it came from, and why."""

    value: float
    provenance: Provenance
    note: str


INPUTS: dict[str, Input] = {
    # --- product facts -------------------------------------------------------
    "borrower_rate": Input(
        0.0999,
        Provenance.PRODUCT,
        "Annual rate charged to the borrower. Advertised by the product.",
    ),
    "max_ltv": Input(
        0.80,
        Provenance.PRODUCT,
        "Maximum borrowable share of portfolio value. Advertised by the product.",
    ),
    "assumed_market_return": Input(
        0.15,
        Provenance.ASSUMED,
        "Nominal annual portfolio return implied by the product's 'keep compounding' "
        "pitch. Used only to size user-side value and retained AUM, never revenue. "
        "A forward return is not knowable; this is a modelling input.",
    ),
    # --- BlinkMoney's own economics -----------------------------------------
    "origination_fee_rate": Input(
        0.015,
        Provenance.ASSUMED,
        "Sourcing/origination fee on disbursed amount, the Lending Service Provider "
        "model under RBI's Digital Lending Guidelines. THE SINGLE LARGEST DRIVER OF "
        "REVENUE HERE AND ENTIRELY ASSUMED — BlinkMoney's actual commercial terms "
        "are not public. Swept in the scenario table because the answer is roughly "
        "linear in it.",
    ),
    "servicing_cost_per_loan": Input(
        180.0,
        Provenance.ASSUMED,
        "Rupees per loan per year for collections, support and reconciliation. "
        "Roughly FIXED per loan regardless of size, which is precisely why a small "
        "ticket is uneconomic and why the industry sets a minimum. This constant is "
        "what makes the micro-SIP constraint bite in the model.",
    ),
    "acquisition_cost_per_disbursal": Input(
        60.0,
        Provenance.ASSUMED,
        "Marginal cost of driving one disbursal via in-app nudges. Low because the "
        "user is already acquired — this is cross-sell, not acquisition.",
    ),
    # --- risk ----------------------------------------------------------------
    "default_rate": Input(
        0.012,
        Provenance.INDUSTRY,
        "Share of disbursed value entering default. LAMF is over-collateralised by "
        "liquid mutual fund units that can be redeemed on a margin call, so realised "
        "default is structurally far below unsecured consumer credit. The 1.2% is "
        "reasoned from that structure, NOT measured from any published portfolio.",
    ),
    "loss_given_default": Input(
        0.25,
        Provenance.INDUSTRY,
        "Share of a defaulted balance actually lost after liquidating collateral. "
        "Low because the collateral is liquid and marked daily; the loss is the gap "
        "between the margin-call trigger and the realised sale price.",
    ),
    "risk_provision_multiple": Input(
        1.5,
        Provenance.ASSUMED,
        "Prudence multiple applied to expected loss when provisioning. Above 1.0 "
        "because expected loss is an average and a correlated market drawdown "
        "triggers margin calls across the whole book at once — the risk this book "
        "carries is not independent across users.",
    ),
}


def input_table() -> pd.DataFrame:
    """Every input with its provenance. Printed on every run and in the README."""
    return pd.DataFrame(
        [
            {"input": name, "value": i.value, "provenance": i.provenance.value, "note": i.note}
            for name, i in INPUTS.items()
        ]
    ).set_index("input")


def value_of(name: str) -> float:
    """Look up an input by name, failing loudly on a typo."""
    if name not in INPUTS:
        raise KeyError(f"unknown input {name!r}; known inputs: {sorted(INPUTS)}")
    return INPUTS[name].value


# =============================================================================
# The model
# =============================================================================


@dataclass
class Scenario:
    """One set of levers. Everything not named here comes from INPUTS."""

    name: str
    eligible_users: int
    disbursal_rate: float
    avg_portfolio_value: float
    ltv_utilisation: float = 0.55
    default_rate: float | None = None
    origination_fee_rate: float | None = None
    sip_break_rate_without_nudge: float = 0.45
    sip_break_rate_with_nudge: float = 0.33
    annual_sip_contribution: float = 7_665.0  # Rs 21/day
    horizon_years: int = 5

    def fee_rate(self) -> float:
        return (
            self.origination_fee_rate
            if self.origination_fee_rate is not None
            else value_of("origination_fee_rate")
        )

    def loss_rate(self) -> float:
        return self.default_rate if self.default_rate is not None else value_of("default_rate")


@dataclass
class Economics:
    """Computed outputs for one scenario."""

    scenario: str
    loans: int
    avg_ticket: float
    loan_book: float
    gross_revenue: float
    expected_loss: float
    risk_provision: float
    servicing_cost: float
    acquisition_cost: float
    net_revenue: float
    net_margin: float
    revenue_per_eligible_user: float
    net_revenue_per_loan: float
    user_value_created: float
    aum_retained: float
    aum_retained_future_value: float
    details: dict = field(default_factory=dict)


def compute(scenario: Scenario) -> Economics:
    """Run the unit economics for one scenario.

    The arithmetic is deliberately flat and readable rather than vectorised —
    this is a model a founder should be able to audit line by line, and every
    intermediate is a quantity someone might want to quote on its own.
    """
    loans = int(round(scenario.eligible_users * scenario.disbursal_rate))
    avg_ticket = scenario.avg_portfolio_value * value_of("max_ltv") * scenario.ltv_utilisation
    loan_book = loans * avg_ticket

    # --- revenue: origination fee only. The interest belongs to the lender. ---
    gross_revenue = loan_book * scenario.fee_rate()

    # --- risk ----------------------------------------------------------------
    expected_loss = loan_book * scenario.loss_rate() * value_of("loss_given_default")
    risk_provision = expected_loss * value_of("risk_provision_multiple")

    # --- costs: servicing is per-loan and fixed, which is the whole problem ---
    servicing_cost = loans * value_of("servicing_cost_per_loan")
    acquisition_cost = loans * value_of("acquisition_cost_per_disbursal")

    net_revenue = gross_revenue - risk_provision - servicing_cost - acquisition_cost
    net_margin = net_revenue / gross_revenue if gross_revenue else 0.0

    # --- value delivered to the USER, explicitly not our revenue -------------
    user_value_created = loan_book * (value_of("assumed_market_return") - value_of("borrower_rate"))

    # --- AUM retained by not breaking the SIP --------------------------------
    # Only the *incremental* saves count. Some users would have kept their SIP
    # anyway, and crediting the nudge with those would inflate the number by
    # roughly three times here.
    breaks_avoided = loans * (
        scenario.sip_break_rate_without_nudge - scenario.sip_break_rate_with_nudge
    )
    aum_retained = breaks_avoided * scenario.annual_sip_contribution
    growth = (1 + value_of("assumed_market_return")) ** scenario.horizon_years
    aum_retained_future_value = aum_retained * growth

    return Economics(
        scenario=scenario.name,
        loans=loans,
        avg_ticket=round(avg_ticket, 2),
        loan_book=round(loan_book, 2),
        gross_revenue=round(gross_revenue, 2),
        expected_loss=round(expected_loss, 2),
        risk_provision=round(risk_provision, 2),
        servicing_cost=round(servicing_cost, 2),
        acquisition_cost=round(acquisition_cost, 2),
        net_revenue=round(net_revenue, 2),
        net_margin=round(net_margin, 4),
        revenue_per_eligible_user=round(
            net_revenue / scenario.eligible_users if scenario.eligible_users else 0.0, 2
        ),
        net_revenue_per_loan=round(net_revenue / loans if loans else 0.0, 2),
        user_value_created=round(user_value_created, 2),
        aum_retained=round(aum_retained, 2),
        aum_retained_future_value=round(aum_retained_future_value, 2),
        details={
            "breaks_avoided": round(breaks_avoided, 1),
            "fee_rate": scenario.fee_rate(),
            "default_rate": scenario.loss_rate(),
            "cost_per_loan": value_of("servicing_cost_per_loan")
            + value_of("acquisition_cost_per_disbursal"),
        },
    )


def breakeven_ticket() -> float:
    """Smallest average ticket at which one loan covers its own fixed costs.

    Servicing and acquisition are per-loan and broadly independent of size, so
    below this ticket every additional loan destroys value no matter how many are
    written. This single number explains the industry's minimum ticket better
    than any argument about credit risk, and it is the number the memo turns on.
    """
    fixed = value_of("servicing_cost_per_loan") + value_of("acquisition_cost_per_disbursal")
    margin_rate = value_of("origination_fee_rate") - (
        value_of("default_rate")
        * value_of("loss_given_default")
        * value_of("risk_provision_multiple")
    )
    if margin_rate <= 0:
        raise ValueError("risk-adjusted fee rate is non-positive; no ticket can break even")
    return fixed / margin_rate


def scenario_table(scenarios: list[Scenario]) -> pd.DataFrame:
    """Compute every scenario into one comparable frame."""
    rows = [compute(s).__dict__ for s in scenarios]
    frame = pd.DataFrame(rows).drop(columns="details").set_index("scenario")
    return frame


def base_optimistic_pessimistic(eligible_users: int, avg_portfolio_value: float) -> list[Scenario]:
    """The standard three cases, swept on the two inputs that matter most.

    Disbursal rate and origination fee are swept together because they are the
    two assumptions with no empirical anchor at all, and revenue is roughly
    linear in both — so the spread between these cases is the honest width of the
    answer, not a decoration.
    """
    return [
        Scenario(
            name="pessimistic",
            eligible_users=eligible_users,
            disbursal_rate=0.18,
            avg_portfolio_value=avg_portfolio_value,
            ltv_utilisation=0.45,
            default_rate=0.030,
            origination_fee_rate=0.010,
            sip_break_rate_with_nudge=0.40,
        ),
        Scenario(
            name="base",
            eligible_users=eligible_users,
            disbursal_rate=0.34,
            avg_portfolio_value=avg_portfolio_value,
            ltv_utilisation=0.55,
        ),
        Scenario(
            name="optimistic",
            eligible_users=eligible_users,
            disbursal_rate=0.50,
            avg_portfolio_value=avg_portfolio_value,
            ltv_utilisation=0.65,
            default_rate=0.008,
            origination_fee_rate=0.020,
            sip_break_rate_with_nudge=0.28,
        ),
    ]


def portfolio_maturity_sweep(
    eligible_users: int, portfolio_values: list[float] | None = None
) -> pd.DataFrame:
    """Economics as the book matures — the axis that actually decides viability.

    Disbursal-rate sweeps are the conventional sensitivity for a lending model
    and are close to irrelevant here: at a Rs 1,000 average portfolio the book
    loses money at *every* disbursal rate, because the loss is per-loan. Growing
    the portfolio is the only lever that crosses breakeven, so it gets its own
    table.
    """
    portfolio_values = portfolio_values or [
        1_000,
        2_500,
        5_000,
        10_000,
        25_000,
        50_000,
        75_000,
        100_000,
    ]
    rows = []
    for value in portfolio_values:
        result = compute(
            Scenario(
                name=f"portfolio_{int(value)}",
                eligible_users=eligible_users,
                disbursal_rate=0.34,
                avg_portfolio_value=value,
            )
        )
        rows.append(
            {
                "avg_portfolio_value": value,
                "avg_ticket": result.avg_ticket,
                "loans": result.loans,
                "gross_revenue": result.gross_revenue,
                "net_revenue": result.net_revenue,
                "net_revenue_per_loan": result.net_revenue_per_loan,
                "viable": result.net_revenue > 0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    logger.info(
        "INPUTS (provenance matters — ASSUMED means no source):\n%s",
        input_table()[["value", "provenance"]].to_string(),
    )

    breakeven = breakeven_ticket()
    logger.info("breakeven average ticket: Rs %s", f"{breakeven:,.0f}")

    # Sized from what phase 2 actually produced on this book.
    eligible_users = 1_912
    observed_avg_portfolio = 1_800.0

    scenarios = base_optimistic_pessimistic(eligible_users, observed_avg_portfolio)
    table = scenario_table(scenarios)
    logger.info(
        "scenarios at the OBSERVED average portfolio of Rs %s:\n%s",
        f"{observed_avg_portfolio:,.0f}",
        table[
            ["loans", "avg_ticket", "loan_book", "gross_revenue", "net_revenue", "net_margin"]
        ].to_string(),
    )

    if (table["net_revenue"] < 0).all():
        logger.warning(
            "EVERY scenario is loss-making at the observed portfolio size. The "
            "constraint is collateral per user, not disbursal rate — see MEMO.md."
        )

    sweep = portfolio_maturity_sweep(eligible_users)
    logger.info("portfolio maturity sweep:\n%s", sweep.to_string(index=False))


if __name__ == "__main__":
    main()
