"""Bounds of the financial models: rate precision, exact debt service, formatting and refusals.

Expected debt service comes from fractions.Fraction, which is exact for the level-payment formula,
or from hand-worked literals. Nothing here derives an expectation from the code under test.
"""

import asyncio
import contextlib
import json
import os
import random
import re
import socket
import sys
import threading
import time
from decimal import Decimal, DivisionByZero, InvalidOperation
from fractions import Fraction
from uuid import uuid4

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from foundry import cli, finance
from foundry.api import create_app
from foundry.finance import acquisition, annual_debt_service, forecast, money, period_rollups, ratio
from foundry.models import DealInputs, ForecastInputs

D = Decimal
PLAIN_MONEY = re.compile(r"-?\d+\.\d{2}")
DEAL = {"target": "Synthetic target", "thesis": "A synthetic acquisition for bounds tests."}
FORECAST_RATES = [
    "monthly_growth",
    "monthly_retention",
    "tax_rate",
    "annual_interest_rate",
    "discount_rate",
    "terminal_growth",
]
DEAL_RATES = ["debt_rate", "seller_rate", "tax_rate"]
TWELVE_PLACES, THIRTEEN_PLACES = "0.012345678901", "0.0123456789012"
# On main, base month 1 of this plan displays balance_check as -0.00 (an unrounded residue below zero).
NEGATIVE_ZERO_PLAN = {
    "months": 12,
    "receivable_days": 0,
    "monthly_capex": "100",
    "asset_life_months": 36,
    "monthly_growth": "0",
}


def exact_service(principal, rate, years) -> str:
    """Level monthly payment × 12 in exact rationals, rounded half-even to cents."""
    p, r, n = Fraction(D(principal)), Fraction(D(rate)), years * 12
    if r == 0:
        value = p / n * 12
    else:
        m = r / 12
        value = p * m / (1 - (1 + m) ** -n) * 12
    cents = value * 100
    whole, rest = divmod(cents.numerator, cents.denominator)
    if 2 * rest > cents.denominator or (2 * rest == cents.denominator and whole % 2):
        whole += 1
    sign = "-" if whole < 0 else ""
    whole = abs(whole)
    return f"{sign}{whole // 100}.{whole % 100:02d}"


def outputs(result):
    """Every string a model result displays, excluding the echoed inputs."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key != "inputs":
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            found.append(node)

    walk(result)
    return found


def no_negative_zero(result):
    return not any(v in {"-0.00", "-0.0000"} for v in outputs(result))


def post(client, bid, path, body, key=None):
    return client.post(
        f"/api/businesses/{bid}/{path}",
        headers={"Idempotency-Key": key or str(uuid4()), "Content-Type": "application/json"},
        content=body if isinstance(body, str) else json.dumps(body),
    )


def counts(store):
    with store.read() as con:
        return {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("objects", "events", "commands")
        }


# 1. A tiny accepted loan rate crashed or gave a wrong payment.


@pytest.mark.parametrize("rate", ["0", "1e-20", "1e-26", "1e-27"])
def test_tiny_rates_give_the_exact_payment(rate):
    # 1,000,000,000,000 / 30 years = 33,333,333,333.33; interest at these rates is far below a cent.
    value = annual_debt_service(D("1000000000000"), D(rate), 30)
    assert money(value) == exact_service("1000000000000", rate, 30) == "33333333333.33"


def test_api_refuses_an_over_precise_loan_rate_and_accepts_the_smallest_rate(client, setup):
    store, _, _, bid, _ = setup
    before = counts(store)
    response = post(client, bid, "deals", {**DEAL, "debt_rate": "1e-27"})
    assert response.status_code == 422
    assert "debt_rate" in response.json()["detail"] and "12 decimal places" in response.json()["detail"]
    assert counts(store) == before
    response = post(client, bid, "deals", {**DEAL, "debt_rate": "1E-12", "senior_debt": "400000"})
    assert response.status_code == 201
    expected = Fraction(D(exact_service("400000", "1E-12", 7))) + Fraction(D(exact_service("0", "0.06", 5)))
    assert Fraction(D(response.json()["data"]["analysis"]["annual_debt_service"])) == expected


# 2. A large result crashed the money formatter.


@pytest.mark.parametrize(
    ("value", "cents", "basis"),
    [
        ("1E+60", "1" + "0" * 60 + ".00", "1" + "0" * 60 + ".0000"),
        ("123456789012345678901234567890123456789012345.675", None, None),
        ("-1E-35", "0.00", "0.0000"),
        ("-0", "0.00", "0.0000"),
        ("0E+60", "0.00", "0.0000"),
        ("1E-400", "0.00", "0.0000"),
        ("-0.005", "0.00", "-0.0050"),
        ("-0.004", "0.00", "-0.0040"),
        ("-0.00004", "0.00", "0.0000"),
        ("0.005", "0.00", "0.0050"),
        ("0.015", "0.02", "0.0150"),
        ("-0.015", "-0.02", "-0.0150"),
        ("9.995", "10.00", "9.9950"),
        ("12.34567", "12.35", "12.3457"),
    ],
)
def test_money_and_ratio_never_raise_and_never_show_negative_zero(value, cents, basis):
    if cents is None:  # A 48-digit value: the half-even cent is worked by hand.
        assert money(D(value)) == "123456789012345678901234567890123456789012345.68"
        assert ratio(D(value)) == "123456789012345678901234567890123456789012345.6750"
        return
    assert money(D(value)) == cents
    assert ratio(D(value)) == basis


def test_narrow_spread_beyond_the_rate_limit_now_formats_at_the_model_level():
    # The validator now refuses this spread; the model itself must still format the huge terminal value.
    discount = D("0.050000000000000000000000000000000000001")  # 0.05 + 1e-39, written out
    inputs = ForecastInputs.model_construct(
        **{**ForecastInputs().model_dump(), "discount_rate": discount, "terminal_growth": D("0.05")}
    )
    dcf = forecast(inputs)["scenarios"]["base"]["summary"]["dcf"]
    assert PLAIN_MONEY.fullmatch(dcf["enterprise_value"]) and len(dcf["enterprise_value"]) > 40
    assert dcf["terminal_share"] == "1.0000"


def test_api_refuses_a_spread_beyond_the_rate_limit(client, setup):
    store, _, _, bid, _ = setup
    before = counts(store)
    body = {
        "inputs": {"terminal_growth": "0.05", "discount_rate": "0.050000000000000000000000000000000000001"}
    }
    response = post(client, bid, "forecasts", body)
    assert response.status_code == 422 and "inputs.discount_rate" in response.json()["detail"]
    assert counts(store) == before


# 3. Zero was shown with a minus sign.


def test_balance_check_residue_displays_as_zero():
    result = forecast(ForecastInputs(**NEGATIVE_ZERO_PLAN))
    assert result["scenarios"]["base"]["months"][0]["balance_check"] == "0.00"
    assert no_negative_zero(result)


def test_api_forecast_shows_no_negative_zero(client, setup):
    bid = setup[3]
    response = post(client, bid, "forecasts", {"inputs": NEGATIVE_ZERO_PLAN})
    assert response.status_code == 201
    assert no_negative_zero(response.json()["data"])


def test_sub_cent_shortfall_is_flagged_but_displays_zero():
    # Documented in FINANCE.md: flags use unrounded amounts.
    plan = forecast(
        ForecastInputs(months=1, opening_equity=0, monthly_units=0, fixed_costs="0.004", unit_cost=0)
    )["scenarios"]["base"]["summary"]
    assert plan["first_shortfall_month"] == 1
    assert plan["minimum_cash"] == "0.00" and plan["additional_cash_needed"] == "0.00"
    deal = acquisition(
        DealInputs(
            **DEAL,
            annual_revenue=100,
            reported_ebitda="0.004",
            senior_debt=0,
            seller_note=0,
            annual_capex="0.008",
            tax_rate=0,
        ),
        set(),
    )["scenarios"]["base"]
    assert deal["cash_shortfall"] is True and deal["cash_after_debt_service"] == "0.00"


def test_yearly_rollup_sums_displayed_months():
    # 0.001 units × 5.00 = 0.005 a month, displayed half-even as 0.00; twelve months total 0.06 unrounded.
    result = forecast(
        ForecastInputs(months=12, monthly_units="0.001", unit_price=5, monthly_growth=0, receivable_days=0)
    )["scenarios"]["base"]
    assert {row["revenue"] for row in result["months"]} == {"0.00"}
    assert period_rollups(result["months"])[0]["revenue"] == "0.00"
    assert result["summary"]["total_revenue"] == "0.06"


# 4. JSON numbers lose digits before validation.


def test_json_numbers_with_too_many_places_are_refused(client, setup):
    store, _, _, bid, _ = setup
    before = counts(store)
    response = post(
        client, bid, "forecasts", '{"inputs": {"monthly_growth": 0.12345678901234567890123456789}}'
    )
    assert response.status_code == 422 and "inputs.monthly_growth" in response.json()["detail"]
    computed = 0.1 + 0.2
    assert repr(computed) == "0.30000000000000004"
    response = post(client, bid, "forecasts", {"inputs": {"monthly_growth": computed}})
    assert response.status_code == 422 and "inputs.monthly_growth" in response.json()["detail"]
    assert counts(store) == before


def test_json_number_and_string_store_identical_inputs_and_results(client, setup):
    bid = setup[3]
    numbers = (
        '{"inputs": {"monthly_growth": 0.03, "tax_rate": 0.25, "unit_price": 2000.5, "discount_rate": 0.2}}'
    )
    strings = '{"inputs": {"monthly_growth": "0.03", "tax_rate": "0.25", "unit_price": "2000.5", "discount_rate": "0.2"}}'
    a, b = (post(client, bid, "forecasts", body) for body in (numbers, strings))
    assert a.status_code == b.status_code == 201
    assert a.json()["data"] == b.json()["data"]
    deal_numbers = json.dumps({**DEAL, "debt_rate": 0.0725, "purchase_price": 812345.67})
    deal_strings = json.dumps({**DEAL, "debt_rate": "0.0725", "purchase_price": "812345.67"})
    a, b = (post(client, bid, "deals", body) for body in (deal_numbers, deal_strings))
    assert a.status_code == b.status_code == 201
    assert a.json()["data"]["inputs"] == b.json()["data"]["inputs"]
    assert a.json()["data"]["analysis"] == b.json()["data"]["analysis"]


def test_json_number_money_keeps_only_its_float_digits(client, setup):
    # Documented, unchanged behaviour: send decimal inputs as strings to keep every digit.
    bid = setup[3]
    response = post(client, bid, "forecasts", '{"inputs": {"unit_price": 1234567890.123456789}}')
    assert response.status_code == 201
    assert response.json()["data"]["inputs"]["unit_price"] == "1234567890.1234567"


# Rate precision limit for every rate-like field.


@pytest.mark.parametrize("field", FORECAST_RATES)
def test_forecast_rates_take_twelve_places_not_thirteen(field):
    # A zero terminal growth keeps a small discount rate above it.
    base = {} if field == "terminal_growth" else {"terminal_growth": "0"}
    assert getattr(ForecastInputs(**base, **{field: TWELVE_PLACES}), field) == D(TWELVE_PLACES)
    with pytest.raises(ValidationError) as caught:
        ForecastInputs(**base, **{field: THIRTEEN_PLACES})
    errors = caught.value.errors()
    assert [e["loc"] for e in errors] == [(field,)] and errors[0]["type"] == "decimal_max_places"


@pytest.mark.parametrize("field", DEAL_RATES)
def test_deal_rates_take_twelve_places_not_thirteen(field):
    assert getattr(DealInputs(**DEAL, **{field: TWELVE_PLACES}), field) == D(TWELVE_PLACES)
    with pytest.raises(ValidationError) as caught:
        DealInputs(**DEAL, **{field: THIRTEEN_PLACES})
    errors = caught.value.errors()
    assert [e["loc"] for e in errors] == [(field,)] and errors[0]["type"] == "decimal_max_places"


@pytest.mark.parametrize(
    ("path", "field"), [("forecasts", f) for f in FORECAST_RATES] + [("deals", f) for f in DEAL_RATES]
)
def test_api_names_the_over_precise_rate_field(client, setup, path, field):
    store, _, _, bid, _ = setup
    base = {} if field == "terminal_growth" else {"terminal_growth": "0"}
    wrap = (lambda x: {"inputs": {**base, **x}}) if path == "forecasts" else (lambda x: {**DEAL, **x})
    assert post(client, bid, path, wrap({field: TWELVE_PLACES})).status_code == 201
    before = counts(store)
    response = post(client, bid, path, wrap({field: THIRTEEN_PLACES}))
    assert response.status_code == 422
    loc = ("inputs." if path == "forecasts" else "") + field
    assert (
        response.json()["detail"] == f"body.{loc}: Decimal input should have no more than 12 decimal places"
    )
    assert counts(store) == before


def test_places_are_counted_on_the_value_not_its_spelling():
    assert ForecastInputs(tax_rate="0.250000000000000000").tax_rate == D("0.25")
    assert ForecastInputs(tax_rate="1E-12").tax_rate == D("1E-12")
    assert ForecastInputs(tax_rate="0E-20").tax_rate == 0
    with pytest.raises(ValidationError):
        ForecastInputs(tax_rate="1E-13")


def test_money_fields_keep_unbounded_digits():
    amount = "123.456789012345678901234567890123456789012"
    assert ForecastInputs(unit_price=amount).unit_price == D(amount)
    assert DealInputs(**DEAL, fees=amount).fees == D(amount)


# Debt service at the corners and across the accepted range.


@pytest.mark.parametrize("principal", ["1000000000000", "0.01", "1E-400", "999999999999.99"])
@pytest.mark.parametrize("rate", ["1E-12", "0.000000000001", "0.999999999999", "1", "0.09", "0.123456789012"])
@pytest.mark.parametrize("years", [1, 30])
def test_debt_service_corners_match_exact_rationals(principal, rate, years):
    assert money(annual_debt_service(D(principal), D(rate), years)) == exact_service(principal, rate, years)


def test_debt_service_hand_worked_literals():
    # 12,000 at 100% for one year: m = 1/12, payment = 1000 × (13/12)^12 / ((13/12)^12 − 1) × 12.
    assert money(annual_debt_service(D("12000"), D("1"), 1)) == exact_service("12000", "1", 1) == "19439.39"
    assert money(annual_debt_service(D("120000"), D("0"), 10)) == "12000.00"
    assert money(annual_debt_service(D("0"), D("0.09"), 7)) == "0.00"


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        ("999999999999.005", "999999999999.00"),  # An exact half cent rounds to even.
        ("999999999999.015", "999999999999.02"),
        ("999999999999.005000000000000000000000000001", "999999999999.01"),  # Just above the half cent.
        ("999999999999.004999999999999999999999999999", "999999999999.00"),
    ],
)
def test_zero_rate_debt_service_rounds_half_cents_exactly(principal, expected):
    assert exact_service(principal, "0", 1) == expected
    assert money(annual_debt_service(D(principal), D("0"), 1)) == expected


def test_random_accepted_debt_service_matches_exact_rationals():
    rng = random.Random(20261111)
    for _ in range(1500):
        places = rng.choice([0, 2, 2, 2, 6, 30])
        principal = D(rng.randint(0, 10**12 * 10**places)).scaleb(-places)
        rate_places = rng.randint(1, 12)
        rate = D(rng.randint(0, 10**rate_places)).scaleb(-rate_places)
        years = rng.randint(1, 30)
        DealInputs(**DEAL, senior_debt=0, debt_rate=rate, purchase_price=principal)  # accepted inputs
        assert money(annual_debt_service(principal, rate, years)) == exact_service(principal, rate, years), (
            principal,
            rate,
            years,
        )


# The extreme accepted forecast and exotic accepted spellings.


EXTREME = {
    "months": 60,
    "opening_equity": "1000000000000",
    "opening_debt": "1000000000000",
    "monthly_units": "100000000",
    "unit_price": "1000000000000",
    "unit_cost": "0",
    "monthly_growth": "0.5",
    "monthly_retention": "1",
    "fixed_costs": "0",
    "annual_interest_rate": "1",
    "monthly_principal": "1000000000000",
    "discount_rate": "0.100000000001",
    "terminal_growth": "0.1",
    "excess_cash": "1000000000000",
}


def test_extreme_accepted_forecast_completes():
    result = forecast(ForecastInputs(**EXTREME))
    for name in ("base", "downside", "upside"):
        dcf = result["scenarios"][name]["summary"]["dcf"]
        assert PLAIN_MONEY.fullmatch(dcf["enterprise_value"]) and len(dcf["enterprise_value"]) > 40
        assert dcf["terminal_share"] == "1.0000"
    assert no_negative_zero(result)
    last = result["scenarios"]["upside"]["months"][-1]
    assert all(PLAIN_MONEY.fullmatch(v) for k, v in last.items() if k not in {"month", "units"})
    assert period_rollups(result["scenarios"]["upside"]["months"])[-1]["period"] == "Year 5"


def test_api_extreme_accepted_forecast_is_saved(client, setup):
    bid = setup[3]
    response = post(client, bid, "forecasts", {"inputs": EXTREME})
    assert response.status_code == 201
    generated = client.post(
        f"/api/businesses/{bid}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        json={"template": "business_plan"},
    )
    assert generated.status_code == 201


@pytest.mark.parametrize("spelling", ["-0", "0E+60", "1E-400"])
def test_exotic_money_spellings_are_evaluated(client, setup, spelling):
    bid = setup[3]
    plan = {
        k: spelling
        for k in (
            "opening_debt",
            "unit_cost",
            "fixed_costs",
            "monthly_capex",
            "monthly_principal",
            "excess_cash",
        )
    }
    response = post(client, bid, "forecasts", {"inputs": plan})
    assert response.status_code == 201 and no_negative_zero(response.json()["data"])
    money_fields = (
        "replacement_management",
        "fees",
        "working_capital",
        "integration_cost",
        "senior_debt",
        "seller_note",
        "annual_capex",
        "annual_working_capital_growth",
        "cash_acquired",
        "debt_assumed",
    )
    deal = {**DEAL, **dict.fromkeys(money_fields, spelling)}
    response = post(client, bid, "deals", deal)
    assert response.status_code == 201
    analysis = response.json()["data"]["analysis"]
    assert no_negative_zero(analysis) and analysis["annual_debt_service"] == "0.00"
    # A principal spelled 1E-400 still runs through the exact payment.
    assert money(annual_debt_service(D(spelling), D("0.09"), 7)) == "0.00"


# Refusals: an arithmetic failure inside a model is a 422 that stores nothing.


def test_arithmetic_failure_is_refused_without_storing_or_consuming_the_key(client, setup, monkeypatch):
    store, service, actor, bid, _ = setup
    real_forecast, real_acquisition = finance.forecast, finance.acquisition

    def broken(error):
        def model(*args):
            raise error("synthetic model failure")

        return model

    monkeypatch.setattr(finance, "forecast", broken(InvalidOperation))
    monkeypatch.setattr(finance, "acquisition", broken(DivisionByZero))
    before = counts(store)
    plan = post(client, bid, "forecasts", {"inputs": {"months": 12}}, key="plan-key")
    deal = post(client, bid, "deals", DEAL, key="deal-key")
    for response, name in ((plan, "InvalidOperation"), (deal, "DivisionByZero")):
        assert response.status_code == 422
        assert response.json()["detail"].startswith("The financial model cannot evaluate these inputs")
        assert name in response.json()["detail"] and "synthetic" not in response.json()["detail"]
    assert counts(store) == before
    assert service.audit(actor, bid)["integrity"]["valid"]

    monkeypatch.setattr(finance, "forecast", real_forecast)
    monkeypatch.setattr(finance, "acquisition", real_acquisition)
    plan = post(client, bid, "forecasts", {"inputs": {"months": 13}}, key="plan-key")
    deal = post(client, bid, "deals", {**DEAL, "fees": "31000"}, key="deal-key")
    assert plan.status_code == deal.status_code == 201
    assert plan.json()["data"]["inputs"]["months"] == 13 and deal.json()["data"]["inputs"]["fees"] == "31000"
    assert service.audit(actor, bid)["integrity"]["valid"]


def test_cli_prints_a_refusal_instead_of_a_traceback(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "cli" / "foundry.db")
    cli.main(["--db", db, "init"])
    business = tmp_path / "business.json"
    business.write_text(json.dumps({"name": "CLI test", "concept": "A synthetic business for a CLI test."}))
    capsys.readouterr()
    cli.main(["--db", db, "create", str(business)])
    bid = json.loads(capsys.readouterr().out)["id"]
    plan, deal = tmp_path / "plan.json", tmp_path / "deal.json"
    deal.write_text(json.dumps(DEAL))

    plan.write_text(json.dumps({"inputs": {"discount_rate": THIRTEEN_PLACES}}))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--db", db, "forecast", bid, str(plan)])
    assert stopped.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Foundry: ") and "discount_rate" in err and "Traceback" not in err

    monkeypatch.setattr(finance, "forecast", lambda *a: D(1) / D(0))
    monkeypatch.setattr(finance, "acquisition", lambda *a: D("1E+999999") * D("1E+999999"))
    plan.write_text(json.dumps({"inputs": {"months": 12}}))
    for command, path in (("forecast", plan), ("acquire", deal)):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["--db", db, command, bid, str(path)])
        assert stopped.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("Foundry: The financial model cannot evaluate these inputs")
        assert "Traceback" not in err


@contextlib.contextmanager
def api_server(store):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(store.path), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_mcp_tools_refuse_like_the_api(setup, monkeypatch):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(owner, {"name": "Bounds agent", "role": "agent", "business_ids": [bid]})
    real_forecast = finance.forecast

    async def check(url):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={**os.environ, "FOUNDRY_API_URL": url, "FOUNDRY_API_TOKEN": agent["token"]},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "analyze_acquisition",
                    {"business_id": bid, "command_key": "mcp-deal", "inputs": {**DEAL, "debt_rate": "1e-27"}},
                )
                assert result.isError and "debt_rate" in result.content[0].text
                result = await client.call_tool(
                    "analyze_acquisition",
                    {"business_id": bid, "command_key": "mcp-deal", "inputs": {**DEAL, "debt_rate": "1E-12"}},
                )
                assert not result.isError
                forecast_call = {
                    "business_id": bid,
                    "command_key": "mcp-plan",
                    "title": "MCP plan",
                    "inputs": {"months": 12},
                    "evidence_ids": [],
                }
                monkeypatch.setattr(finance, "forecast", lambda *a: D(1) / D(0))
                result = await client.call_tool("build_forecast", forecast_call)
                assert result.isError
                assert "The financial model cannot evaluate these inputs" in result.content[0].text
                monkeypatch.setattr(finance, "forecast", real_forecast)
                result = await client.call_tool("build_forecast", forecast_call)
                assert not result.isError

    before = counts(store)
    with api_server(store) as url:
        asyncio.run(check(url))
    after = counts(store)
    # Only the two successful commands were stored (a deal adds its diligence tasks).
    assert after["commands"] == before["commands"] + 2
    assert service.audit(owner, bid)["integrity"]["valid"]
