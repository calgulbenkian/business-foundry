from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from foundry.finance import acquisition, annual_debt_service, forecast, period_rollups
from foundry.models import Adjustment, DealInputs, ForecastInputs


def simple(**changes):
    defaults = dict(
        months=3,
        opening_equity=1000,
        monthly_units=10,
        unit_price=100,
        unit_cost=20,
        monthly_growth=0,
        fixed_costs=100,
        receivable_days=0,
        tax_rate=0,
    )
    return ForecastInputs(**(defaults | changes))


def test_three_statements_against_independent_hand_calculation():
    result = forecast(simple())["scenarios"]["base"]
    row = result["months"][0]
    assert row["revenue"] == "1000.00"
    assert row["ebitda"] == "700.00"
    assert row["net_income"] == "700.00"
    assert row["cash"] == "1700.00"
    assert row["equity"] == "1700.00"
    assert row["balance_check"] == "0.00"
    assert result["summary"]["ending_cash"] == "3100.00"
    assert result["summary"]["statements_balance"]


@pytest.mark.parametrize(
    "delay,receipts,ar",
    [
        (0, ["1000.00", "1000.00", "1000.00"], "0.00"),
        (15, ["500.00", "1000.00", "1000.00"], "500.00"),
        (30, ["0.00", "1000.00", "1000.00"], "1000.00"),
        (45, ["0.00", "500.00", "1000.00"], "1500.00"),
        (60, ["0.00", "0.00", "1000.00"], "2000.00"),
        (90, ["0.00", "0.00", "0.00"], "3000.00"),
    ],
)
def test_credit_collections_conserve_revenue(delay, receipts, ar):
    rows = forecast(simple(receivable_days=delay))["scenarios"]["base"]["months"]
    assert [r["receipts"] for r in rows] == receipts
    assert rows[-1]["receivables"] == ar
    assert sum(D(r["receipts"]) for r in rows) + D(ar) == 3000
    assert all(D(r["balance_check"]) == 0 for r in rows)


def test_debt_interest_principal_and_depreciation_link_to_cash_and_balance_sheet():
    row = forecast(
        simple(
            months=1,
            opening_debt=1200,
            annual_interest_rate="0.12",
            monthly_principal=100,
            monthly_capex=120,
            asset_life_months=12,
            tax_rate="0.25",
        )
    )["scenarios"]["base"]["months"][0]
    assert row["interest"] == "12.00"
    assert row["depreciation"] == "10.00"
    assert row["tax"] == "169.50"  # (700 - 10 - 12) * .25
    assert row["net_income"] == "508.50"
    assert row["cash"] == "2498.50"  # 2200 + 1000 - 200 - 100 - 12 - 169.5 - 120 - 100
    assert row["net_fixed_assets"] == "110.00"
    assert row["debt"] == "1100.00"
    assert row["equity"] == "1508.50"
    assert row["balance_check"] == "0.00"


def test_negative_cash_is_not_silently_financed_and_tax_losses_not_refunded():
    result = forecast(simple(opening_equity=0, monthly_units=0, fixed_costs=100, tax_rate=".25"))[
        "scenarios"
    ]["base"]
    assert result["summary"]["additional_cash_needed"] == "300.00"
    assert result["summary"]["first_shortfall_month"] == 1
    assert result["months"][-1]["debt"] == "0.00"
    assert all(r["tax"] == "0.00" for r in result["months"])


def test_long_lived_assets_and_debt_never_go_negative():
    rows = forecast(
        simple(months=60, opening_debt=100, monthly_principal=500, monthly_capex=100, asset_life_months=3)
    )["scenarios"]["base"]["months"]
    assert all(D(r["debt"]) >= 0 and D(r["net_fixed_assets"]) >= 0 for r in rows)
    assert rows[-1]["depreciation"] == "100.00"
    assert all(abs(D(r["balance_check"])) < D(".01") for r in rows)


def test_discounted_cash_flow_and_equity_bridge():
    model = simple(months=12, discount_rate=".20", terminal_growth=0, opening_debt=300, excess_cash=50)
    dcf = forecast(model)["scenarios"]["base"]["summary"]["dcf"]
    pv = sum(700 / 1.2 ** ((i + 1) / 12) for i in range(12))
    terminal_pv = 8400 / 0.2 / 1.2
    assert abs(float(dcf["enterprise_value"]) - pv - terminal_pv) < 0.01
    assert D(dcf["equity_value"]) == D(dcf["enterprise_value"]) - 250
    assert forecast(simple(months=11))["scenarios"]["base"]["summary"]["dcf"] is None
    assert forecast(simple(months=12, monthly_units=0))["scenarios"]["base"]["summary"]["dcf"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"discount_rate": ".01", "terminal_growth": ".02"},
        {"monthly_growth": "NaN"},
        {"unit_price": "Infinity"},
        {"months": 0},
        {"receivable_days": -1},
    ],
)
def test_invalid_forecast_inputs_rejected(changes):
    with pytest.raises(ValidationError):
        simple(**changes)


def deal(**changes):
    return DealInputs(
        **(dict(target="Example target", thesis="Acquire predictable delivery capacity.") | changes)
    )


def test_acquisition_only_supported_adjustments_and_sources_equal_uses():
    model = deal(
        replacement_management=50000,
        adjustments=[
            Adjustment(label="Verified one-off", amount=10000, evidence_id="ok"),
            Adjustment(label="Unsupported synergy", amount=50000, evidence_id="not-reviewed"),
        ],
    )
    result = acquisition(model, {"ok"})
    assert result["normalized_ebitda"] == "160000.00"
    assert len(result["accepted_adjustments"]) == 1
    assert len(result["excluded_adjustments"]) == 1
    assert result["enterprise_value_range"] == ["480000.00", "800000.00"]
    s = result["sources_and_uses"]
    assert D(s["senior_debt"]) + D(s["seller_note"]) + D(s["buyer_equity_required"]) == D(s["total_uses"])
    assert D(result["scenarios"]["downside"]["ebitda"]) == 60000


def test_amortization_zero_interest_and_no_debt_case():
    assert annual_debt_service(D(120000), D(0), 10) == 12000
    assert abs(float(annual_debt_service(D(400000), D(".09"), 7)) - 77227.575648) < 0.01
    result = acquisition(deal(senior_debt=0, seller_note=0), set())
    assert result["scenarios"]["base"]["coverage_ratio"] is None


def test_negative_earnings_do_not_produce_positive_multiple_valuation():
    result = acquisition(deal(reported_ebitda=-20000), set())
    assert result["enterprise_value_range"] is None
    assert result["scenarios"]["base"]["cash_shortfall"]


@pytest.mark.parametrize(
    "changes",
    [{"senior_debt": 1000000}, {"multiple_low": 8, "multiple_high": 3}, {"reported_ebitda": 2000000}],
)
def test_inconsistent_deal_inputs_rejected(changes):
    with pytest.raises(ValidationError):
        deal(**changes)


def test_period_rollups_sum_flows_and_close_balances():
    rows = forecast(simple(months=14))["scenarios"]["base"]["months"]
    year, tail = period_rollups(rows)
    assert year["period"] == "Year 1"
    assert year["revenue"] == "12000.00"
    assert year["net_income"] == "8400.00"
    assert year["cash"] == "9400.00"
    assert tail["period"] == "Months 13\u201314"
    assert tail["revenue"] == "2000.00"
    assert tail["cash"] == "10800.00"
    assert tail["equity"] == "10800.00"
    assert [r["period"] for r in period_rollups(rows[:13])] == ["Year 1", "Month 13"]
    assert period_rollups([]) == []
