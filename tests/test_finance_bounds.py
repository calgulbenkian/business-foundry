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
import typing
from decimal import (
    MAX_EMAX,
    MIN_ETINY,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Subnormal,
    Underflow,
    localcontext,
)
from fractions import Fraction
from uuid import uuid4

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ValidationError

from foundry import cli, finance
from foundry.api import create_app
from foundry.demo import seed
from foundry.finance import (
    _fixed,
    acquisition,
    annual_debt_service,
    forecast,
    money,
    period_rollups,
    ratio,
    total_annual_debt_service,
)
from foundry.models import Adjustment, DealInputs, ForecastCreate, ForecastInputs

D = Decimal
PLAIN_MONEY = re.compile(r"-?\d+\.\d{2}")
# The target's figures and price are required; these are the values the model once filled in for them.
REQUIRED_FIGURES = {"annual_revenue": "1000000", "reported_ebitda": "200000", "purchase_price": "800000"}
DEAL = {
    "target": "Synthetic target",
    "thesis": "A synthetic acquisition for bounds tests.",
    **REQUIRED_FIGURES,
}


def deal_inputs(**changes) -> DealInputs:
    """DealInputs for DEAL, with any of its fields replaced."""
    return DealInputs(**(DEAL | changes))


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


def exact_payment(principal, rate, years) -> Fraction:
    """Level monthly payment × 12 in exact rationals."""
    p, r, n = Fraction(D(principal)), Fraction(D(rate)), years * 12
    if r == 0:
        return p / n * 12
    m = r / 12
    return p * m / (1 - (1 + m) ** -n) * 12


def half_even(value: Fraction, places: int = 2) -> str:
    """An exact rational rounded half-even to fixed places, with no minus sign on zero."""
    scaled = value * 10**places
    whole, rest = divmod(scaled.numerator, scaled.denominator)
    if 2 * rest > scaled.denominator or (2 * rest == scaled.denominator and whole % 2):
        whole += 1
    sign = "-" if whole < 0 else ""
    whole = abs(whole)
    return f"{sign}{whole // 10**places}.{whole % 10**places:0{places}d}"


def exact_service(principal, rate, years) -> str:
    """Level monthly payment × 12 in exact rationals, rounded half-even to cents."""
    return half_even(exact_payment(principal, rate, years))


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
        deal_inputs(
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
    assert getattr(deal_inputs(**{field: TWELVE_PLACES}), field) == D(TWELVE_PLACES)
    with pytest.raises(ValidationError) as caught:
        deal_inputs(**{field: THIRTEEN_PLACES})
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


def test_money_fields_refuse_digits_beyond_twelve_places():
    amount = "123.456789012345678901234567890123456789012"
    for build in (lambda x: ForecastInputs(unit_price=x), lambda x: deal_inputs(fees=x)):
        with pytest.raises(ValidationError) as caught:
            build(amount)
        assert caught.value.errors()[0]["type"] == "decimal_max_places"
    assert ForecastInputs(unit_price=amount[:16]).unit_price == D("123.456789012345")
    assert deal_inputs(fees=amount[:16]).fees == D("123.456789012345")


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


def random_decimal(rng, high: int, places: int, low: int = 0) -> Decimal:
    # Built from an integer string, so no context rounds the intended value.
    return D(f"{rng.randint(low * 10**places, high * 10**places)}E-{places}")


def test_random_accepted_debt_service_matches_exact_rationals():
    rng = random.Random(20261111)
    for _ in range(1500):
        principal = random_decimal(rng, 10**12, rng.choice([0, 2, 2, 2, 6, 12]))
        rate = random_decimal(rng, 1, rng.randint(1, 12))
        years = rng.randint(1, 30)
        deal_inputs(senior_debt=0, debt_rate=rate, purchase_price=principal)  # accepted inputs
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


@pytest.mark.parametrize("spelling", ["-0", "0E+60", "1E-12"])
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
    # The smallest accepted principal still runs through the exact payment.
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


# Every decimal input carries at most 12 decimal places (one rule, driven from the models' own fields).

EXTREME_ZEROS = [
    "0E+999999999999999999",
    "-0E+999999999999999999",
    "0E-999999999999999999",
    "-0E-999999999999999999",
]
PLACES_MESSAGE = "Decimal input should have no more than 12 decimal places"
MODEL_MESSAGE = "The financial model cannot evaluate these inputs"


def holds(annotation, kind) -> bool:
    """Whether a field annotation is `kind` or contains it at any depth (Optional, list, Annotated...)."""
    return annotation is kind or any(holds(arg, kind) for arg in typing.get_args(annotation))


def decimal_fields(model):
    return [name for name, field in model.model_fields.items() if holds(field.annotation, Decimal)]


def nested_models(model, found=None) -> list:
    """`model` and every model reachable through its fields' annotations, to any depth, in first-seen order."""
    found = [] if found is None else found
    if model not in found:
        found.append(model)

        def visit(annotation):
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                nested_models(annotation, found)
            for arg in typing.get_args(annotation):
                visit(arg)

        for field in model.model_fields.values():
            visit(field.annotation)
    return found


def field_case(model, field, value):
    """A body that is valid apart from `field`, the model that validates it, and the field's location."""
    if model is ForecastInputs:
        inputs = {} if field == "terminal_growth" else {"terminal_growth": "0"}
        if field == "fixed_cost_step":
            inputs["fixed_cost_step_month"] = 1
        return {**inputs, field: value}, (field,)
    if model is Adjustment:
        return {**DEAL, "adjustments": [{"label": "Synthetic", field: value}]}, ("adjustments", 0, field)
    assert model is DealInputs, f"No field case for {model.__name__}"
    body = {**DEAL, field: value}
    if field == "multiple_high":
        body["multiple_low"] = "0"
    if field == "annual_revenue":
        body["reported_ebitda"] = "0"
    if field == "purchase_price":
        body["senior_debt"] = "0"
    return body, (field,)


DECIMAL_MODELS = [m for top in (ForecastInputs, DealInputs) for m in nested_models(top) if decimal_fields(m)]
DECIMAL_FIELDS = [(m, f) for m in DECIMAL_MODELS for f in decimal_fields(m)]


def test_every_model_with_a_decimal_input_has_a_field_case():
    # A decimal field in a further nested model joins DECIMAL_FIELDS and must be given a case in field_case.
    assert DECIMAL_MODELS == [ForecastInputs, DealInputs, Adjustment]
    assert holds(list[typing.Annotated[Decimal | None, "x"]], Decimal)


def test_the_field_lists_find_every_known_decimal_input():
    found = {(m.__name__, f) for m, f in DECIMAL_FIELDS}
    assert {("ForecastInputs", f) for f in FORECAST_RATES + ["unit_price", "monthly_units"]} <= found
    assert {
        ("DealInputs", f) for f in DEAL_RATES + ["senior_debt", "reported_ebitda", "multiple_low"]
    } <= found
    assert ("Adjustment", "amount") in found


@pytest.mark.parametrize(("model", "field"), DECIMAL_FIELDS, ids=lambda x: getattr(x, "__name__", x))
def test_every_decimal_input_takes_twelve_places_not_thirteen(model, field):
    top = DealInputs if model is Adjustment else model
    body, loc = field_case(model, field, TWELVE_PLACES)
    top.model_validate(body)
    body, loc = field_case(model, field, THIRTEEN_PLACES)
    with pytest.raises(ValidationError) as caught:
        top.model_validate(body)
    errors = caught.value.errors()
    assert [e["loc"] for e in errors] == [loc] and errors[0]["type"] == "decimal_max_places"


@pytest.mark.parametrize(("model", "field"), DECIMAL_FIELDS, ids=lambda x: getattr(x, "__name__", x))
def test_api_names_every_over_precise_decimal_input(client, setup, model, field):
    store, _, _, bid, _ = setup
    path, prefix = ("forecasts", "inputs.") if model is ForecastInputs else ("deals", "")

    def wrap(body):
        return {"inputs": body} if model is ForecastInputs else body

    assert post(client, bid, path, wrap(field_case(model, field, TWELVE_PLACES)[0])).status_code == 201
    before = counts(store)
    body, loc = field_case(model, field, THIRTEEN_PLACES)
    response = post(client, bid, path, wrap(body))
    assert response.status_code == 422
    assert response.json()["detail"] == f"body.{prefix}{'.'.join(map(str, loc))}: {PLACES_MESSAGE}"
    assert counts(store) == before


def test_a_json_number_amount_with_more_float_places_is_refused(client, setup):
    store, _, _, bid, _ = setup
    before = counts(store)
    computed = 1000.1 + 0.2
    assert repr(computed) == "1000.3000000000001"
    response = post(client, bid, "forecasts", {"inputs": {"unit_price": computed}})
    assert response.json()["detail"] == f"body.inputs.unit_price: {PLACES_MESSAGE}"
    response = post(client, bid, "deals", {**DEAL, "fees": computed})
    assert response.json()["detail"] == f"body.fees: {PLACES_MESSAGE}"
    assert counts(store) == before


# Finding 1: a zero spelled with an extreme exponent is a zero.


@pytest.mark.parametrize("zero", EXTREME_ZEROS)
def test_api_saves_a_zero_with_an_extreme_exponent(client, setup, zero):
    bid = setup[3]
    response = post(
        client,
        bid,
        "deals",
        {"target": "Synthetic", "thesis": "Synthetic testing", **REQUIRED_FIGURES, "senior_debt": zero},
    )
    assert response.status_code == 201
    analysis = response.json()["data"]["analysis"]
    assert analysis["annual_debt_service"] == "0.00" and analysis["sources_and_uses"]["senior_debt"] == "0.00"
    assert analysis["scenarios"]["base"]["coverage_ratio"] is None and no_negative_zero(analysis)
    plan = {"months": 12, "opening_debt": zero, "unit_cost": zero, "monthly_capex": zero, "tax_rate": zero}
    response = post(client, bid, "forecasts", {"inputs": plan})
    assert response.status_code == 201
    data = response.json()["data"]
    first = data["scenarios"]["base"]["months"][0]
    assert first["debt"] == first["cost_of_sales"] == first["capex"] == first["tax"] == "0.00"
    assert no_negative_zero(data)


@pytest.mark.parametrize(
    "zero", [*EXTREME_ZEROS, f"0E{MIN_ETINY}", f"-0E{MIN_ETINY}", f"0E+{MAX_EMAX}", f"-0E+{MAX_EMAX}"]
)
def test_fixed_formats_zeros_at_the_decimal_exponent_limits(zero):
    assert _fixed(D(zero), D("0.01")) == money(D(zero)) == "0.00"
    assert _fixed(D(zero), D("0.0001")) == ratio(D(zero)) == "0.0000"


def test_the_exponent_limits_are_the_decimal_limits():
    for beyond in (f"0E{MIN_ETINY - 1}", f"0E+{MAX_EMAX + 1}"):
        with pytest.raises(InvalidOperation):
            D(beyond)


@pytest.mark.parametrize("value", [f"1E{MIN_ETINY}", f"-1E{MIN_ETINY}", "-9E-999999999999999999"])
def test_fixed_formats_the_smallest_nonzero_decimals_as_zero(value):
    assert money(D(value)) == "0.00" and ratio(D(value)) == "0.0000"


def test_fixed_refuses_only_a_value_too_long_to_write_and_never_with_value_error():
    # A fixed-point form of 10^(10^18) would be 10^18 characters; the refusal is an ArithmeticError.
    with pytest.raises(InvalidOperation):
        money(D(f"1E+{MAX_EMAX}"))
    assert money(D("1E+1000")) == "1" + "0" * 1000 + ".00"


# Finding 2: the total of two loans is exact to the cent.

FINDING_2_PRINCIPAL = "500000000000.0025000000000000000000000001"


def exact_total(*loans) -> Fraction:
    return sum((exact_payment(*loan) for loan in loans), Fraction(0))


def test_combined_debt_service_of_the_finding_principals_is_exact():
    loans = [(FINDING_2_PRINCIPAL, "0", 1), (FINDING_2_PRINCIPAL, "0", 1)]
    expected = exact_total(*loans)
    assert expected == Fraction(D("1000000000000.0050000000000000000000000002"))
    assert half_even(expected) == "1000000000000.01"
    total = total_annual_debt_service(*((D(p), D(r), y) for p, r, y in loans))
    assert money(total) == "1000000000000.01"
    # Each payment alone is still rounded once from its exact value.
    assert money(annual_debt_service(D(FINDING_2_PRINCIPAL), D(0), 1)) == "500000000000.00"


def test_api_refuses_the_finding_2_principals(client, setup):
    store, _, _, bid, _ = setup
    before = counts(store)
    body = {
        "target": "Synthetic",
        "thesis": "Synthetic testing",
        **REQUIRED_FIGURES,
        "purchase_price": "1000000000000",
        "senior_debt": FINDING_2_PRINCIPAL,
        "seller_note": FINDING_2_PRINCIPAL,
        "debt_rate": "0",
        "seller_rate": "0",
        "debt_years": 1,
        "seller_years": 1,
    }
    response = post(client, bid, "deals", body)
    assert response.status_code == 422
    assert f"body.senior_debt: {PLACES_MESSAGE}" in response.json()["detail"]
    assert counts(store) == before


def test_accepted_loans_whose_exact_total_is_a_half_cent_round_once(client, setup):
    # 0.01 / 3 + 0.035 / 3 = 0.015 exactly; each part alone has no finite decimal. Half-even gives 0.02,
    # and the cash after debt service of 1 − 0.015 = 0.985 gives 0.98. Adding rounded parts gives 0.01.
    loans = [("0.01", "0", 3), ("0.035", "0", 3)]
    assert exact_total(*loans) == Fraction(15, 1000)
    body = {
        **DEAL,
        "annual_revenue": "1",
        "reported_ebitda": "1",
        "senior_debt": "0.01",
        "seller_note": "0.035",
        "debt_rate": "0",
        "seller_rate": "0",
        "debt_years": 3,
        "seller_years": 3,
        "annual_capex": "0",
        "tax_rate": "0",
    }
    response = post(client, setup[3], "deals", body)
    assert response.status_code == 201
    analysis = response.json()["data"]["analysis"]
    assert analysis["annual_debt_service"] == half_even(Fraction(15, 1000)) == "0.02"
    base = analysis["scenarios"]["base"]
    assert base["cash_after_debt_service"] == half_even(1 - Fraction(15, 1000)) == "0.98"
    assert base["coverage_ratio"] == half_even(1 / Fraction(15, 1000), 4) == "66.6667"


def test_random_pairs_of_accepted_loans_total_exactly():
    rng = random.Random(20261112)
    for _ in range(400):
        loans = []
        terms = rng.sample(range(1, 31), 2)
        for years in terms:
            principal = random_decimal(rng, 10**12, rng.choice([0, 2, 2, 6, 12]), low=0)
            rate = random_decimal(rng, 1, rng.randint(1, 12))
            loans.append((principal or D("0.01"), rate or D("0.05"), years))
        (p1, r1, y1), (p2, r2, y2) = loans
        expected = half_even(exact_total(*loans))
        assert money(total_annual_debt_service(*loans)) == expected, loans
        deal = deal_inputs(
            purchase_price="1000000000000",
            fees="1000000000000",
            senior_debt=p1,
            debt_rate=r1,
            debt_years=y1,
            seller_note=p2,
            seller_rate=r2,
            seller_years=y2,
        )
        assert acquisition(deal, set())["annual_debt_service"] == expected, loans


# Finding 3: a principal below the places limit is refused by validation, not by the model.


@pytest.mark.parametrize("principal", ["1E-1000000", "1E-13"])
def test_api_refuses_a_principal_beyond_twelve_places(client, setup, principal):
    store, _, _, bid, _ = setup
    before = counts(store)
    response = post(
        client,
        bid,
        "deals",
        {"target": "Synthetic", "thesis": "Synthetic testing", **REQUIRED_FIGURES, "senior_debt": principal},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == f"body.senior_debt: {PLACES_MESSAGE}"
    assert MODEL_MESSAGE not in response.json()["detail"]
    assert counts(store) == before


def test_api_evaluates_the_smallest_accepted_principal(client, setup):
    response = post(
        client,
        setup[3],
        "deals",
        {"target": "Synthetic", "thesis": "Synthetic testing", **REQUIRED_FIGURES, "senior_debt": "1E-12"},
    )
    assert response.status_code == 201
    analysis = response.json()["data"]["analysis"]
    service = exact_payment("1E-12", "0.09", 7)
    assert analysis["annual_debt_service"] == "0.00"
    # Default deal: earnings 200,000; interest 1E-12 × 0.09; tax at 25%; capex 15,000.
    available = Fraction(200000) - (Fraction(200000) - Fraction(D("9E-14"))) / 4 - 15000
    assert analysis["scenarios"]["base"]["cash_available_for_debt_service"] == half_even(available)
    assert analysis["scenarios"]["base"]["coverage_ratio"] == half_even(available / service, 4)
    assert analysis["scenarios"]["base"]["cash_after_debt_service"] == half_even(available - service)


# Domain corners: the smallest positive and the largest accepted amounts evaluate, and every non-zero value
# the forecast computes in the caller's context, and every exact step of the acquisition screen, stays within
# 1E-100 <= |v| < 1E+46 (docs/FINANCE.md). The exact fractions over the debt service (the payment's numerator and
# denominator, and cash available scaled by that denominator) have no limit on size; they are checked against
# exact rationals and the digit bounds of STEP_DIGITS instead.

TINY = "1E-12"
FORECAST_MAXIMA = {
    "opening_equity": "1000000000000",
    "opening_debt": "1000000000000",
    "monthly_units": "100000000",
    "unit_price": "1000000000000",
    "unit_cost": "1000000000000",
    "monthly_growth": "0.5",
    "monthly_retention": "1",
    "fixed_costs": "1000000000000",
    "fixed_cost_step": "1000000000000",
    "monthly_capex": "1000000000000",
    "tax_rate": "1",
    "annual_interest_rate": "1",
    "monthly_principal": "1000000000000",
    "discount_rate": "1",
    "terminal_growth": "0.1",
    "excess_cash": "1000000000000",
}
FORECAST_CORNERS = {
    "smallest": {
        **dict.fromkeys(decimal_fields(ForecastInputs), TINY),
        "terminal_growth": "0",
        "fixed_cost_step_month": 1,
        "months": 60,
        "receivable_days": 45,
        "asset_life_months": 240,
    },
    "smallest_with_signed_minima": {
        **dict.fromkeys(decimal_fields(ForecastInputs), TINY),
        "monthly_growth": "-0.8",
        "terminal_growth": "-0.2",
        "fixed_cost_step_month": 1,
        "months": 60,
        "receivable_days": 45,
        "asset_life_months": 240,
    },
    "largest": {
        **FORECAST_MAXIMA,
        "fixed_cost_step_month": 1,
        "months": 60,
        "receivable_days": 90,
        "asset_life_months": 1,
    },
    "largest_margin_narrowest_spread": EXTREME,
}
ADJUSTMENTS = 50  # The most a deal accepts.
DEAL_CORNERS = {
    "smallest": {
        **dict.fromkeys(decimal_fields(DealInputs), TINY),
        "debt_years": 30,
        "seller_years": 30,
        "adjustments": [{"label": "Synthetic", "amount": TINY, "evidence_id": "reviewed"}] * ADJUSTMENTS,
    },
    "largest": {
        **dict.fromkeys(decimal_fields(DealInputs), "1000000000000"),
        "reported_ebitda": "1000000000",
        "multiple_low": "100",
        "multiple_high": "100",
        "debt_rate": "1",
        "seller_rate": "1",
        "tax_rate": "1",
        "debt_years": 1,
        "seller_years": 1,
        "adjustments": [{"label": "Synthetic", "amount": "1000000000", "evidence_id": "reviewed"}]
        * ADJUSTMENTS,
    },
    "largest_coverage": {
        **dict.fromkeys(decimal_fields(DealInputs), "1000000000000"),
        "reported_ebitda": "-1000000000",
        "multiple_low": "0",
        "multiple_high": "100",
        "senior_debt": TINY,
        "debt_rate": "0",
        "debt_years": 30,
        "seller_note": "0",
        "seller_rate": "0",
        "tax_rate": "1",
        "adjustments": [{"label": "Synthetic", "amount": "-1000000000", "evidence_id": "reviewed"}]
        * ADJUSTMENTS,
    },
}


@contextlib.contextmanager
def bounded_magnitudes():
    """Any non-zero result outside 1E-100 ≤ |v| < 1E+46 raises (Subnormal below, Overflow above)."""
    with localcontext() as ctx:
        ctx.Emin, ctx.Emax = -100, 45
        for signal in (Overflow, Underflow, Subnormal):
            ctx.traps[signal] = True
        yield


def displayed(result):
    values = [v for v in outputs(result) if re.fullmatch(r"-?\d+\.\d+", v)]
    assert all(PLAIN_MONEY.fullmatch(v) or re.fullmatch(r"-?\d+\.\d{4}", v) for v in values)
    return [Fraction(D(v)) for v in values]


@pytest.mark.parametrize("corner", FORECAST_CORNERS)
def test_forecast_corners_evaluate_within_the_stated_magnitudes(corner):
    inputs = ForecastInputs(**FORECAST_CORNERS[corner])
    with bounded_magnitudes():
        result = forecast(inputs)
    values = displayed(result)
    assert values and all(abs(v) < 10**46 for v in values) and no_negative_zero(result)
    for name in ("base", "downside", "upside"):
        assert period_rollups(result["scenarios"][name]["months"])


@pytest.mark.parametrize("corner", DEAL_CORNERS)
def test_deal_corners_evaluate_within_the_stated_magnitudes(corner):
    inputs = deal_inputs(**DEAL_CORNERS[corner])
    with bounded_magnitudes():
        result = acquisition(inputs, {"reviewed"})
    values = displayed(result)
    assert values and all(abs(v) < 10**46 for v in values) and no_negative_zero(result)
    loans = [(inputs.senior_debt, inputs.debt_rate, inputs.debt_years)]
    loans.append((inputs.seller_note, inputs.seller_rate, inputs.seller_years))
    assert result["annual_debt_service"] == half_even(exact_total(*loans))
    # The acquisition screen calculates in its own exact contexts, so its steps are checked directly.
    steps = finance._deal_values(inputs, {"reviewed"})
    linear = [steps[k] for k in ("normalized", "uses", "buyer_equity", "interest")]
    linear += [*steps.get("value_range", ()), *steps.get("equity_range", ())]
    linear += [s[k] for s in steps["scenarios"].values() for k in ("earnings", "taxable", "tax", "available")]
    assert all(v.is_zero() or D("1E-100") <= v.copy_abs() < D("1E+46") for v in linear)
    assert_exact(inputs)


def test_corner_outputs_match_the_stated_extremes():
    # The largest coverage ratio: cash of about −3.15E+12 over the smallest service, 1E-12 / 30 years.
    deal = acquisition(deal_inputs(**DEAL_CORNERS["largest_coverage"]), {"reviewed"})
    coverage = Fraction(D(deal["scenarios"]["downside"]["coverage_ratio"]))
    assert -(10**26) < coverage < -(10**25)
    # The largest money output: the enterprise value with the narrowest spread, below 1E+44.
    plan = forecast(ForecastInputs(**EXTREME))["scenarios"]["upside"]["summary"]["dcf"]
    assert 10**42 < Fraction(D(plan["enterprise_value"])) < 10**44
    # The smallest non-zero outputs are one cent and one basis point.
    tiny = acquisition(deal_inputs(**DEAL_CORNERS["smallest"]), {"reviewed"})
    assert tiny["annual_debt_service"] == "0.00" and tiny["normalized_ebitda"] == "0.00"


def test_random_accepted_inputs_stay_within_the_stated_magnitudes():
    rng = random.Random(20261113)
    for _ in range(60):
        plan = {
            k: random_decimal(rng, 1, 12) for k in ("monthly_retention", "tax_rate", "annual_interest_rate")
        }
        plan |= {
            k: random_decimal(rng, 10 ** rng.randint(0, 12), rng.choice([0, 2, 12]))
            for k in (
                "opening_equity",
                "opening_debt",
                "unit_price",
                "unit_cost",
                "fixed_costs",
                "monthly_capex",
            )
        }
        plan |= {
            "monthly_units": random_decimal(rng, 10 ** rng.randint(0, 8), rng.choice([0, 2, 12])),
            "monthly_growth": random_decimal(rng, 5, 11, low=-8) / 10,
            "discount_rate": D("0.2"),
            "terminal_growth": random_decimal(rng, 1, 11, low=-2) / 10,
            "months": rng.randint(1, 60),
            "receivable_days": rng.randint(0, 90),
        }
        with bounded_magnitudes():
            forecast(ForecastInputs(**plan))


# Other callers: zeros with extreme exponents and records saved before the places limit.


def generate(client, bid, template, deal_id=None):
    response = client.post(
        f"/api/businesses/{bid}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        json={"template": template, "deal_id": deal_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


def check_reads(client, setup, deal_id):
    _, service, actor, bid, _ = setup
    snapshot = client.get(f"/api/businesses/{bid}")
    assert snapshot.status_code == 200
    kinds = {o["kind"] for o in snapshot.json()["objects"]}
    assert {"deal", "financial_plan"} <= kinds
    assert client.get(f"/api/businesses/{bid}/export").status_code == 200
    plan_doc = generate(client, bid, "business_plan")
    memo = generate(client, bid, "investment_memo", deal_id)
    download = client.get(f"/api/businesses/{bid}/documents/{memo['id']}/download")
    assert download.status_code == 200 and "Annual debt service" in download.text
    assert plan_doc["data"]["content"]
    assert client.get(f"/api/businesses/{bid}/audit").json()["integrity"]["valid"]
    assert service.audit(actor, bid)["integrity"]["valid"]
    return download.text


AMOUNT_FIELDS_SHOWN_IN_DOCUMENTS = ("replacement_management", "cash_acquired", "debt_assumed", "seller_note")


@pytest.mark.parametrize("zero", ["0E+999999999999999999", "-0E-999999999999999999"])
def test_records_with_extreme_zero_amounts_list_export_render_and_audit(client, setup, zero):
    bid = setup[3]
    plan = {"months": 12, "opening_debt": zero, "monthly_capex": zero, "excess_cash": zero, "tax_rate": zero}
    assert post(client, bid, "forecasts", {"inputs": plan}).status_code == 201
    deal = {**DEAL, **dict.fromkeys(AMOUNT_FIELDS_SHOWN_IN_DOCUMENTS, zero)}
    deal["adjustments"] = [{"label": "Synthetic", "amount": zero}]
    response = post(client, bid, "deals", deal)
    assert response.status_code == 201
    assert response.json()["data"]["inputs"]["seller_note"] == zero
    check_reads(client, setup, response.json()["id"])


def test_cli_saves_and_renders_extreme_zero_amounts(tmp_path, capsys):
    db = str(tmp_path / "cli" / "foundry.db")
    cli.main(["--db", db, "init"])
    business = tmp_path / "business.json"
    business.write_text(json.dumps({"name": "CLI test", "concept": "A synthetic business for a CLI test."}))
    capsys.readouterr()
    cli.main(["--db", db, "create", str(business)])
    bid = json.loads(capsys.readouterr().out)["id"]
    plan, deal = tmp_path / "plan.json", tmp_path / "deal.json"
    plan.write_text(json.dumps({"inputs": dict.fromkeys(("opening_debt", "unit_cost"), EXTREME_ZEROS[0])}))
    deal.write_text(json.dumps({**DEAL, "seller_note": EXTREME_ZEROS[3], "senior_debt": EXTREME_ZEROS[1]}))
    cli.main(["--db", db, "forecast", bid, str(plan)])
    cli.main(["--db", db, "acquire", bid, str(deal)])
    printed, decoder = capsys.readouterr().out, json.JSONDecoder()
    _, end = decoder.raw_decode(printed)
    saved, _ = decoder.raw_decode(printed, end + 1)
    assert saved["data"]["analysis"]["annual_debt_service"] == "0.00"
    cli.main(["--db", db, "document", bid, "investment_memo", "--deal-id", saved["id"]])
    cli.main(["--db", db, "verify", bid])
    assert '"valid": true' in capsys.readouterr().out


def legacy(model):
    """A validator as it was before the places limit: bounds still apply, any number of places is kept."""

    def construct(cls, values):
        defaults = {
            k: f.get_default(call_default_factory=True)
            for k, f in cls.model_fields.items()
            if not f.is_required()
        }
        decimal = set(decimal_fields(cls))
        given = {k: D(v) if k in decimal else v for k, v in values.items()}
        if cls is DealInputs:
            given["adjustments"] = [construct(Adjustment, a) for a in values.get("adjustments", [])]
        return cls.model_construct(**(defaults | given))

    class Legacy:
        @staticmethod
        def model_validate(payload):
            with pytest.raises(ValidationError):
                model.model_validate(payload)
            return construct(model, payload)

    return Legacy


def test_records_saved_before_the_places_limit_stay_usable(client, setup, monkeypatch):
    store, service, actor, bid, _ = setup
    precise = "1234.5678901234567890123"
    plan = {
        "title": "Saved before the limit",
        "inputs": {**ForecastInputs().model_dump(mode="json"), "unit_price": precise, "months": 12},
        "evidence_ids": [],
    }
    deal = {
        **deal_inputs().model_dump(mode="json"),
        "purchase_price": "1000000000000",
        "senior_debt": FINDING_2_PRINCIPAL,
        "seller_note": FINDING_2_PRINCIPAL,
        "debt_rate": "0",
        "seller_rate": "0",
        "debt_years": 1,
        "seller_years": 1,
        "replacement_management": precise,
        "adjustments": [{"label": "Synthetic", "amount": precise, "evidence_id": None}],
    }
    monkeypatch.setattr("foundry.service.ForecastInputs", legacy(ForecastInputs))
    monkeypatch.setattr("foundry.service.DealInputs", legacy(DealInputs))
    saved_plan = service.save_forecast(actor, bid, str(uuid4()), plan)
    saved = service.save_deal(actor, bid, str(uuid4()), deal)
    monkeypatch.undo()
    assert saved_plan["data"]["inputs"]["unit_price"] == precise
    assert saved["data"]["inputs"]["senior_debt"] == FINDING_2_PRINCIPAL
    # Evaluated by the current model, the stored total is exact to the cent.
    assert saved["data"]["analysis"]["annual_debt_service"] == "1000000000000.01"

    memo = check_reads(client, setup, saved["id"])
    assert "1,234.57" in memo
    exported = client.get(f"/api/businesses/{bid}/export").json()
    assert any(json.dumps(o).count(FINDING_2_PRINCIPAL) for o in exported["objects"])

    before = counts(store)
    response = post(client, bid, "deals", {**deal, "adjustments": []})
    assert response.status_code == 422 and f"body.senior_debt: {PLACES_MESSAGE}" in response.json()["detail"]
    response = post(client, bid, "forecasts", plan)
    assert response.json()["detail"] == f"body.inputs.unit_price: {PLACES_MESSAGE}"
    assert counts(store) == before


# The demonstration seed and the example files are accepted.


EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def test_example_files_are_accepted_and_evaluate():
    with open(os.path.join(EXAMPLES, "forecast.json")) as f:
        plan = ForecastCreate.model_validate_json(f.read())
    with open(os.path.join(EXAMPLES, "acquisition.json")) as f:
        deal = DealInputs.model_validate_json(f.read())
    assert no_negative_zero(forecast(plan.inputs))
    assert no_negative_zero(acquisition(deal, set()))


def test_demo_seed_is_accepted(setup):
    _, service, actor, _, _ = setup
    business = seed(service, actor)
    assert service.audit(actor, business["id"])["integrity"]["valid"]


# Every acquisition figure is the exact value of its formula rounded half-even once (docs/FINANCE.md).
#
# exact_figures is the independent calculation: fractions.Fraction, the level-payment formula of exact_payment
# and half_even. The deals below are built to sit on, or one unit in the 24th or 36th decimal place either side
# of, a rounding boundary of a displayed figure.

TAX_STEP = Fraction(1, 10**12)  # A tax rate of 1E-12 moves the tax term 12 places below the earnings.
FIGURES = (
    "normalized_ebitda",
    "enterprise_value_range",
    "equity_value_range",
    "sources_and_uses",
    "annual_debt_service",
    "scenarios",
)
REVIEWED = frozenset({"reviewed"})


def exact_figures(inputs: DealInputs, verified=REVIEWED) -> dict:
    f = {name: Fraction(getattr(inputs, name)) for name in decimal_fields(DealInputs)}
    normalized = f["reported_ebitda"] - f["replacement_management"]
    for item in inputs.adjustments:
        if item.evidence_id and item.evidence_id in verified:
            normalized += Fraction(item.amount)
    service = exact_total(
        (inputs.senior_debt, inputs.debt_rate, inputs.debt_years),
        (inputs.seller_note, inputs.seller_rate, inputs.seller_years),
    )
    interest = f["senior_debt"] * f["debt_rate"] + f["seller_note"] * f["seller_rate"]
    revenue = f["annual_revenue"]
    scenarios = {}
    for name, earnings in (
        ("base", normalized),
        ("downside", normalized - revenue / 10),
        ("upside", normalized + revenue / 20),
    ):
        tax = max(earnings - interest, Fraction(0)) * f["tax_rate"]
        available = earnings - tax - f["annual_capex"] - f["annual_working_capital_growth"]
        after = available - service
        scenarios[name] = {
            "ebitda": half_even(earnings),
            "cash_available_for_debt_service": half_even(available),
            "cash_after_debt_service": half_even(after),
            "coverage_ratio": half_even(available / service, 4) if service else None,
            "cash_shortfall": after < 0,
        }
    values = [normalized * f["multiple_low"], normalized * f["multiple_high"]]
    bridge = f["cash_acquired"] - f["debt_assumed"]
    uses = f["purchase_price"] + f["fees"] + f["working_capital"] + f["integration_cost"]
    return {
        "normalized_ebitda": half_even(normalized),
        "enterprise_value_range": [half_even(v) for v in values] if normalized > 0 else None,
        "equity_value_range": [half_even(v + bridge) for v in values] if normalized > 0 else None,
        "sources_and_uses": {
            "purchase_price": half_even(f["purchase_price"]),
            "fees": half_even(f["fees"]),
            "working_capital": half_even(f["working_capital"]),
            "integration_cost": half_even(f["integration_cost"]),
            "total_uses": half_even(uses),
            "senior_debt": half_even(f["senior_debt"]),
            "seller_note": half_even(f["seller_note"]),
            "buyer_equity_required": half_even(uses - f["senior_debt"] - f["seller_note"]),
        },
        "annual_debt_service": half_even(service),
        "scenarios": scenarios,
    }


def assert_exact(inputs: DealInputs, verified=REVIEWED):
    result = acquisition(inputs, set(verified))
    assert {k: result[k] for k in FIGURES} == exact_figures(inputs, verified), inputs.model_dump(mode="json")
    return result


def significant(value: Decimal) -> int:
    """Significant digits of a Decimal's value, from the first non-zero digit to the last."""
    digits = value.as_tuple().digits  # A non-zero coefficient has no leading zero.
    end = len(digits)
    while end and not digits[end - 1]:
        end -= 1
    return end


def dec(value: Fraction) -> Decimal:
    """The Decimal equal to a rational whose denominator is a power of ten (written from integers)."""
    for places in range(41):
        scaled = value * 10**places
        if scaled.denominator == 1:
            return D(f"{scaled.numerator}E-{places}")
    raise AssertionError(value)


def random_loan(rng, rate: bool):
    places = rng.choice([0, 2, 12])
    principal = D(f"{rng.randint(1, 10**7 * 10**places)}E-{places}")
    rate_value = D(f"{rng.randint(1, 25 * 10**10)}E-12") if rate else D(0)  # Up to 0.25, 12 places.
    return principal, rate_value, rng.randint(1, 30)


LOAN_KINDS = ("one at zero", "one at a rate", "two at zero", "two at rates", "one of each")


def random_loans(rng, kind):
    rates = {
        "one at zero": [False],
        "one at a rate": [True],
        "two at zero": [False, False],
        "two at rates": [True, True],
        "one of each": [False, True],
    }[kind]
    loans = [random_loan(rng, r) for r in rates]
    return loans + [(D(0), D("0.06"), 5)] * (2 - len(loans))


def interest_of(loans) -> Fraction:
    return sum((Fraction(p) * Fraction(r) for p, r, _ in loans), Fraction(0))


def service_of(loans) -> Fraction:
    return exact_total(*loans)


def deal_with_available(rng, loans, target: Fraction, adjust: bool = False) -> DealInputs:
    """A deal whose exact base cash available for debt service is `target`.

    With a tax rate of 1E-12, cash available = R − (R − interest) × 1E-12 − capex for reported EBITDA R. The
    last 12 places of R are chosen so that R − R × 1E-12 meets target − interest × 1E-12 in places 13 to 24;
    capex takes the rest. target − interest × 1E-12 must have at most 24 places.
    """
    (s, r1, y1), (n, r2, y2) = loans
    interest = interest_of(loans)
    rest = (target - interest * TAX_STEP) * 10**24
    assert rest.denominator == 1
    rest = rest.numerator
    whole = int(max(target, interest, Fraction(0))) + rng.randint(2, 1000)
    reported = whole * 10**12 + (-rest) % 10**12
    capex, remainder = divmod(reported * 10**12 - reported - rest, 10**12)
    assert remainder == 0 and capex >= 0
    reported = Fraction(reported, 10**12)
    assert interest < reported <= 10**9
    adjustments = []
    if adjust:  # Part of the earnings arrives as an accepted adjustment; an unreviewed one is excluded.
        amount = Fraction(rng.randint(-(10**20), 10**20), 10**12)
        reported -= amount
        adjustments = [
            {"label": "Synthetic reviewed", "amount": dec(amount), "evidence_id": "reviewed"},
            {"label": "Synthetic unreviewed", "amount": D(rng.randint(1, 10**6)), "evidence_id": "other"},
        ]
    deal = deal_inputs(
        annual_revenue=D(max(whole, int(reported)) + 1 + rng.randint(0, 10**6)),
        reported_ebitda=dec(reported),
        adjustments=adjustments,
        purchase_price=dec(Fraction(s) + Fraction(n)),
        fees=0,
        working_capital=0,
        senior_debt=s,
        debt_rate=r1,
        debt_years=y1,
        seller_note=n,
        seller_rate=r2,
        seller_years=y2,
        annual_capex=dec(Fraction(capex, 10**12)),
        tax_rate=D("1E-12"),
    )
    return deal


def beside(boundary: Fraction, interest: Fraction) -> dict:
    """Reachable cash available values one unit in the 24th place of R's term below and above `boundary`, and
    on it when it is reachable."""
    shift = interest * TAX_STEP
    scaled = (boundary - shift) * 10**24
    low = scaled.numerator // scaled.denominator
    sides = (
        {"below": low - 1, "on": low, "above": low + 1} if scaled == low else {"below": low, "above": low + 1}
    )
    return {side: Fraction(v, 10**24) + shift for side, v in sides.items()}


def boundary_cases(seed, count, boundary_for):
    """Deals on and either side of a boundary that `boundary_for(rng, service)` picks for random loans."""
    rng = random.Random(seed)
    cases = []
    while len(cases) < count:
        loans = random_loans(rng, LOAN_KINDS[len(cases) % len(LOAN_KINDS)])
        boundary = boundary_for(rng, service_of(loans))
        if boundary is None or abs(boundary) > 8 * 10**8:
            continue
        for side, target in beside(boundary, interest_of(loans)).items():
            cases.append((side, target, deal_with_available(rng, loans, target, adjust=len(cases) % 3 == 0)))
    return cases


def check_cases(cases, check=None):
    for side, target, deal in cases:
        result = assert_exact(deal)
        base = result["scenarios"]["base"]
        assert base["cash_available_for_debt_service"] == half_even(target)
        if check:
            check(side, target, deal, base)


def test_cash_after_debt_service_beside_a_half_cent_is_exact():
    def half_cent(rng, service):
        return service + Fraction(2 * rng.randint(-(10**9), 10**9) + 1, 200)

    check_cases(boundary_cases(20261201, 120, half_cent))


def test_shortfall_flag_beside_the_exact_payment_is_exact():
    def check(side, target, deal, base):
        assert base["cash_shortfall"] is (side == "below")

    check_cases(boundary_cases(20261202, 120, lambda rng, service: service), check)


def test_coverage_beside_a_tie_is_exact():
    def tie(rng, service):
        return service * Fraction(2 * rng.randint(-40000, 200000) + 1, 20000)

    check_cases(boundary_cases(20261203, 120, tie))


def test_coverage_exactly_on_a_tie_rounds_half_even():
    # Zero-rate loans whose payment has no finite decimal, with a coverage whose cash available does.
    rng = random.Random(20261204)
    cases = 0
    while cases < 60:
        loans = random_loans(rng, ("one at zero", "two at zero")[cases % 2])
        service = service_of(loans)
        odd = service.denominator
        for prime in (2, 5):
            while odd % prime == 0:
                odd //= prime
        if odd == 1:
            continue
        limit = 8 * 10**8 * 20000 // (service * odd)
        if limit < 1:
            continue
        coverage = Fraction(odd * (2 * rng.randint(0, min(limit, 10**6) // 2) + 1), 20000)
        coverage *= rng.choice([1, -1])
        target = service * coverage
        if (target * 10**24).denominator != 1 or abs(target) > 8 * 10**8:
            continue
        deal = deal_with_available(rng, loans, target, adjust=cases % 3 == 0)
        base = assert_exact(deal)["scenarios"]["base"]
        assert base["coverage_ratio"] == half_even(coverage, 4)
        assert base["coverage_ratio"][-1] in "02468"  # A tie rounds to the even fourth place.
        cases += 1


def test_cash_available_of_more_than_28_digits_beside_a_half_cent_is_exact():
    def half_cent(rng, service):
        return Fraction(2 * rng.randint(10**7, 8 * 10**10) + 1, 200) * rng.choice([1, -1])

    rng = random.Random(20261205)
    cases = []
    while len(cases) < 120:
        kinds = ("no loan", *LOAN_KINDS)
        kind = kinds[len(cases) % len(kinds)]
        loans = random_loans(rng, kind) if kind != "no loan" else [(D(0), D(0), 1)] * 2
        boundary = half_cent(rng, None)
        for side, target in beside(boundary, interest_of(loans)).items():
            cases.append((side, target, deal_with_available(rng, loans, target, adjust=len(cases) % 3 == 0)))
    assert all(significant(dec(target)) > 28 for side, target, _ in cases if side != "on")
    check_cases(cases)


def deal_with_value_beside_a_half_cent(rng, side) -> tuple[DealInputs, Fraction]:
    """A deal whose enterprise value at one multiple is one to 10,000 units in the 24th place below, on or above
    a half cent: normalized × multiple has 34 to 37 digits."""
    while True:
        multiple = rng.randint(10**12, 100 * 10**12)  # 1 to 100 with 12 places, coprime to 10.
        if multiple % 2 and multiple % 5:
            break
    offset = {"below": -rng.randint(1, 10**4), "on": 0, "above": rng.randint(1, 10**4)}[side]
    modulus = 10**22  # In units of 1E-24, one cent is 10^22.
    scaled = (5 * 10**21 + offset) * pow(multiple, -1, modulus) % modulus + rng.randint(0, 3) * modulus
    normalized = Fraction(scaled, 10**12)
    whole = int(normalized // 999999999)
    reported = normalized - whole * 999999999
    adjustments = [{"label": "Synthetic", "amount": "999999999", "evidence_id": "reviewed"}] * whole
    adjustments.append({"label": "Synthetic unsupported", "amount": "1000", "evidence_id": None})
    other = rng.randint(0, multiple) if rng.random() < 0.5 else rng.randint(multiple, 100 * 10**12)
    low, high = sorted([multiple, other])
    deal = deal_inputs(
        annual_revenue="1000000000000",
        reported_ebitda=dec(reported),
        adjustments=adjustments,
        multiple_low=D(f"{low}E-12"),
        multiple_high=D(f"{high}E-12"),
        cash_acquired=D(f"{rng.randint(0, 10**14)}E-2"),
        debt_assumed=D(f"{rng.randint(0, 10**14)}E-2"),
    )
    return deal, normalized * Fraction(multiple, 10**12)


def test_enterprise_and_equity_value_beside_a_half_cent_are_exact():
    rng = random.Random(20261206)
    for case in range(150):
        side = ("below", "on", "above")[case % 3]
        deal, value = deal_with_value_beside_a_half_cent(rng, side)
        cents = value * 100
        distance = cents - cents.numerator // cents.denominator - Fraction(1, 2)  # From the half cent.
        assert {"below": distance < 0, "on": distance == 0, "above": distance > 0}[side]
        assert abs(distance) <= Fraction(10**4, 10**22) and (side == "on" or significant(dec(value)) > 28)
        result = assert_exact(deal)
        assert half_even(value) in result["enterprise_value_range"]


def random_deal(rng, wide: bool) -> DealInputs:
    """A random accepted deal: ordinary amounts with cents and short rates, or 12-place inputs and large amounts."""

    def amount(high, places=None):
        places = rng.choice([0, 2, 12]) if wide and places is None else places if places is not None else 2
        return D(f"{rng.randint(0, high * 10**places)}E-{places}")

    def rate(high_percent):
        places = rng.randint(1, 12) if wide else rng.randint(2, 4)
        return D(f"{rng.randint(0, high_percent * 10**places // 100)}E-{places}")

    top = 10**12 if wide else 10**7  # The largest transaction amount.
    scale = 10**9 if wide else 10**6  # The size of the earnings.
    reported = D(f"{rng.randint(-(scale // 4) * 10**12, scale * 10**12)}E-12") if wide else amount(scale)
    revenue = max(amount(top), reported)
    adjustments = [
        {
            "label": "Synthetic",
            "amount": dec(Fraction(amount(scale // 10)) - Fraction(amount(scale // 10))),
            "evidence_id": rng.choice(["reviewed", "other", None]),
        }
        for _ in range(rng.randint(0, 4))
    ]
    uses = [amount(top if wide else 10 * scale) for _ in range(4)]
    total = sum((Fraction(u) for u in uses), Fraction(0))
    places = 12 if wide else 2

    def share(whole: Fraction) -> Decimal:  # A random part of `whole`, cut to the deal's places.
        return D(f"{int(whole * rng.randint(0, 1000) * 10**places // 1000)}E-{places}")

    senior = share(min(total * Fraction(3, 5), Fraction(10**12)))
    seller = share(min(total - Fraction(senior), Fraction(10**12)))
    low = amount(20, rng.choice([0, 2, 12]) if wide else 1)
    return deal_inputs(
        annual_revenue=revenue,
        reported_ebitda=reported,
        adjustments=adjustments,
        replacement_management=amount(scale // 10),
        multiple_low=low,
        multiple_high=dec(Fraction(low) + Fraction(amount(20, 1))),
        purchase_price=uses[0],
        fees=uses[1],
        working_capital=uses[2],
        integration_cost=uses[3],
        senior_debt=senior,
        debt_rate=rate(25) if rng.random() < 0.8 else D(0),
        debt_years=rng.randint(1, 30),
        seller_note=seller,
        seller_rate=rate(25) if rng.random() < 0.8 else D(0),
        seller_years=rng.randint(1, 30),
        annual_capex=amount(scale // 5),
        annual_working_capital_growth=amount(scale // 20),
        tax_rate=rate(40),
        cash_acquired=amount(top // 10),
        debt_assumed=amount(top // 10),
    )


@pytest.mark.parametrize("wide", [False, True], ids=["ordinary", "twelve_places"])
def test_random_deals_are_exact_in_every_figure(wide):
    rng = random.Random(20261207 + wide)
    for _ in range(200):
        assert_exact(random_deal(rng, wide))


# The reviewer's request and hand-worked coverage on a tie: 100,000 over 6 years at zero rate pays 16,666.66…
# a year, so coverage is EBITDA × 6 / 100,000 with no tax, capex or interest. Downside subtracts 100,000 of the
# default 1,000,000 revenue and upside adds 50,000.
TIE_REQUEST = {
    "target": "Synthetic",
    "thesis": "Synthetic testing",
    **REQUIRED_FIGURES,
    "senior_debt": "100000",
    "debt_rate": "0",
    "debt_years": 6,
    "seller_note": "0",
    "annual_capex": "0",
    "tax_rate": "0",
}


@pytest.mark.parametrize(
    ("ebitda", "base", "downside", "upside"),
    [
        ("20837.5", "1.2502", "-4.7498", "4.2502"),  # Exact: 1.25025, −4.74975, 4.25025.
        ("20842.5", "1.2506", "-4.7494", "4.2506"),  # Exact: 1.25055, −4.74945, 4.25055.
    ],
)
def test_api_coverage_on_a_tie_rounds_half_even(client, setup, ebitda, base, downside, upside):
    response = post(client, setup[3], "deals", {**TIE_REQUEST, "reported_ebitda": ebitda})
    assert response.status_code == 201
    scenarios = response.json()["data"]["analysis"]["scenarios"]
    assert [scenarios[name]["coverage_ratio"] for name in ("base", "downside", "upside")] == [
        base,
        downside,
        upside,
    ]


def test_cash_after_debt_service_of_exactly_zero_is_not_a_shortfall():
    # 0.01 / 3 + 0.035 / 3 = 0.015 exactly, and cash available is 0.015 with no tax, capex or interest.
    loans = {"senior_debt": "0.01", "seller_note": "0.035", "debt_rate": "0", "seller_rate": "0"}
    body = {**DEAL, **loans, "debt_years": 3, "seller_years": 3, "annual_capex": "0", "tax_rate": "0"}
    base = assert_exact(DealInputs(**(body | {"reported_ebitda": "0.015"})))["scenarios"]["base"]
    assert base["cash_after_debt_service"] == "0.00" and base["cash_shortfall"] is False
    assert base["coverage_ratio"] == "1.0000"
    base = assert_exact(DealInputs(**(body | {"reported_ebitda": "0.014999999999"})))["scenarios"]["base"]
    assert base["cash_after_debt_service"] == "0.00" and base["cash_shortfall"] is True
    assert base["coverage_ratio"] == "1.0000"


def synthetic(**fields):
    defaults = dict.fromkeys(
        ("fees", "working_capital", "integration_cost", "annual_working_capital_growth"), "0"
    ) | {"cash_acquired": "0", "debt_assumed": "0", "adjustments": [], "replacement_management": "0"}
    return {
        "target": "Synthetic target",
        "thesis": "Synthetic thesis for a probe.",
        **REQUIRED_FIGURES,
        "multiple_low": "3",
        "multiple_high": "5",
        "tax_rate": "0.000000000001",
        **defaults,
        **fields,
    }


# Requests that 1.0.1 as first proposed got wrong, one per family, with the exact base figure.
LOANS_A = {"senior_debt": "16546575.67", "debt_rate": "0", "debt_years": 9}
LOANS_A |= {"seller_note": "68625107.58", "seller_rate": "0.104148", "seller_years": 21}
LOANS_B = {"senior_debt": "82792299.35", "debt_rate": "0.090412", "debt_years": 29}
LOANS_B |= {"seller_note": "74626943.49", "seller_rate": "0.015372", "seller_years": 20}
EXAMPLE_REQUESTS = [
    (
        synthetic(
            annual_revenue="25067710",
            reported_ebitda="25067709.975",
            purchase_price="868145801",
            senior_debt="73209800",
            debt_rate="0",
            debt_years=30,
            seller_note="794936000",
            seller_rate="0",
            seller_years=30,
            annual_capex="0",
            tax_rate="0",
        ),
        {"coverage_ratio": "0.8662"},
    ),
    (
        synthetic(
            annual_revenue="21633440",
            reported_ebitda="21633439.94025922",
            purchase_price="56969554.32",
            senior_debt="44193993.07",
            debt_rate="0.168104",
            debt_years=13,
            seller_note="12775560.25",
            seller_rate="0.20573",
            seller_years=11,
            annual_capex="10306760.003621810658",
        ),
        {"cash_shortfall": True},
    ),
    (
        synthetic(
            annual_revenue="17313842",
            reported_ebitda="17313841.985945158",
            purchase_price="85171684.25",
            annual_capex="7414875.305182901753",
            **LOANS_A,
        ),
        {"cash_shortfall": False},
    ),
    (
        synthetic(
            annual_revenue="21136961",
            reported_ebitda="21136960.80752734",
            purchase_price="157419243.84",
            annual_capex="8722130.884771204582",
            **LOANS_B,
        ),
        {"cash_after_debt_service": "0.17"},
    ),
    (
        synthetic(
            annual_revenue="17842046",
            reported_ebitda="17842045.985945158",
            purchase_price="85171684.25",
            annual_capex="7943079.220182373549",
            **LOANS_A,
        ),
        {"cash_after_debt_service": "0.09"},
    ),
    (
        synthetic(
            annual_revenue="44722704",
            reported_ebitda="44722703.20391642",
            purchase_price="157419243.84",
            annual_capex="9504934.916968584527",
            **LOANS_B,
        ),
        {"coverage_ratio": "2.8367"},
    ),
    (
        synthetic(
            annual_revenue="52027158",
            reported_ebitda="52027157.21177942",
            purchase_price="89498772.41",
            senior_debt="67899117.1",
            debt_rate="0.111838",
            debt_years=15,
            seller_note="21599654.31",
            seller_rate="0.234875",
            seller_years=7,
            annual_capex="12908371.33336604644",
        ),
        {"coverage_ratio": "2.4969"},
    ),
    (
        synthetic(
            annual_revenue="555579562",
            reported_ebitda="555579561.000000000656",
            purchase_price="1",
            senior_debt="0",
            debt_rate="0",
            debt_years=7,
            seller_note="0",
            seller_rate="0",
            seller_years=5,
            annual_capex="554747270.984444421095",
        ),
        {"cash_available_for_debt_service": "832290.01", "cash_after_debt_service": "832290.01"},
    ),
]


@pytest.mark.parametrize(("body", "expected"), EXAMPLE_REQUESTS)
def test_example_requests_give_the_exact_figures(body, expected):
    assert len(body) == 23  # Every input of the original request is written out.
    base = assert_exact(DealInputs.model_validate_json(json.dumps(body)))["scenarios"]["base"]
    assert {k: base[k] for k in expected} == expected


# D3: the largest number of significant digits an accepted input can give each exact step (docs/FINANCE.md).
# Money inputs are at most 1E+12 and earnings inputs at most 1E+9 in size, rates at most 1, multiples at most 100,
# each with at most 12 places, so each step's span of digit positions is bounded. Two loans of 30 years, with
# s = 12 + rate ≤ 13 to 12 places: each denominator s^360 − 12^360 < 10^402 with 4,320 places.
STEP_DIGITS = {
    "normalized": 25,  # Below 1.051E+12 in size, 12 places.
    "uses": 25,
    "buyer_equity": 25,
    "interest": 37,  # Below 2E+12, 24 places.
    "earnings": 27,  # Upside: below 1.051E+12 in size, 14 places (5% of revenue).
    "taxable": 37,  # Below 3.2E+12 in size, 24 places.
    "tax": 48,  # Below 1.01E+11, 36 places.
    "available": 49,  # Below 3.2E+12 in size, 36 places.
    "value_range": 37,  # Below 5.1E+12, 24 places.
    "equity_range": 37,  # Below 6.1E+12, 24 places.
    "denominator": 9443,  # Below 13^720 < 10^803, 8,640 places.
    "numerator": 9479,  # Below 2E+12 × 13^720 < 10^815, 8,664 places.
    "scaled_available": 9491,  # Below 3.2E+12 × 13^720 < 10^815, 8,676 places.
    "after_numerator": 9491,  # Below 5.2E+12 × 13^720 < 10^815, 8,676 places.
}
TWELVE_NINES = "0.999999999999"
WIDEST = "999999999999.999999999999"
# Witnesses that reach the bounds: wide negative earnings with two 30-year loans, and wide positive earnings
# with a tax rate just below 1 and wide capex.
DIGIT_WITNESSES = {
    "negative": {
        "annual_revenue": WIDEST,
        "reported_ebitda": "-1",
        "replacement_management": WIDEST,
        "adjustments": [
            {"label": "Synthetic", "amount": "-999999999.999999999999", "evidence_id": "reviewed"}
        ]
        * ADJUSTMENTS,
        "purchase_price": "1000000000000",
        "fees": "1000000000000",
        "senior_debt": WIDEST,
        "debt_rate": TWELVE_NINES,
        "debt_years": 30,
        "seller_note": WIDEST,
        "seller_rate": TWELVE_NINES,
        "seller_years": 30,
        "annual_capex": WIDEST,
        "annual_working_capital_growth": WIDEST,
        "tax_rate": TWELVE_NINES,
    },
    "positive": {
        "annual_revenue": WIDEST,
        "reported_ebitda": "999999999.999999999999",
        "adjustments": [{"label": "Synthetic", "amount": "999999999.999999999999", "evidence_id": "reviewed"}]
        * ADJUSTMENTS,
        "multiple_low": "99.999999999999",
        "multiple_high": "99.999999999999",
        **dict.fromkeys(("purchase_price", "fees", "working_capital", "integration_cost"), WIDEST),
        "senior_debt": "0.000000000001",
        "debt_rate": TWELVE_NINES,
        "debt_years": 30,
        "seller_note": "0.000000000001",
        "seller_rate": TWELVE_NINES,
        "seller_years": 30,
        "annual_capex": WIDEST,
        "annual_working_capital_growth": WIDEST,
        "tax_rate": TWELVE_NINES,
        "cash_acquired": WIDEST,
        "debt_assumed": "0.000000000001",
    },
}


def step_digits(inputs: DealInputs, verified=REVIEWED) -> dict:
    values = finance._deal_values(inputs, set(verified))
    found = {k: significant(values[k]) for k in ("normalized", "uses", "buyer_equity", "interest")}
    for k in ("numerator", "denominator"):
        found[k] = significant(values[k])
    for k in ("value_range", "equity_range"):
        found[k] = max((significant(v) for v in values.get(k, ())), default=0)
    for k in ("earnings", "taxable", "tax", "available", "scaled_available", "after_numerator"):
        found[k] = max(significant(s[k]) for s in values["scenarios"].values())
    return found


def test_witnesses_reach_the_stated_digit_bounds():
    found = [step_digits(deal_inputs(**w)) for w in DIGIT_WITNESSES.values()]
    assert {k: max(f[k] for f in found) for k in STEP_DIGITS} == STEP_DIGITS


def digit_bound_cases():
    yield from (deal_inputs(**w) for w in DIGIT_WITNESSES.values())
    yield from (deal_inputs(**c) for c in DEAL_CORNERS.values())
    for wide in (False, True):
        rng = random.Random(20261208 + wide)
        yield from (random_deal(rng, wide) for _ in range(100))
    yield from (deal for _, _, deal in boundary_cases(20261209, 30, lambda rng, service: service))


def test_no_accepted_input_exceeds_the_stated_digit_bounds():
    for inputs in digit_bound_cases():
        found = step_digits(inputs)
        assert all(found[k] <= STEP_DIGITS[k] for k in STEP_DIGITS), (found, inputs.model_dump(mode="json"))


def test_exceeding_a_digit_bound_is_caught():
    # A 13-place rate, which validation refuses, needs one digit more for the interest than the stated bound.
    inputs = deal_inputs(**DIGIT_WITNESSES["negative"])
    over = inputs.model_copy(update={"debt_rate": D("0.9999999999999")})
    assert step_digits(inputs)["interest"] == STEP_DIGITS["interest"] < step_digits(over)["interest"]


# D4: the largest and smallest accepted coverage ratios, rounded to four places with their own precision.


def test_largest_and_smallest_accepted_coverage_are_exact():
    # Most negative: downside cash of −1.051E+12 − 1E+11 − 2E+12 = −3.151E+12 over 1E-12 / 30 a year.
    smallest = assert_exact(deal_inputs(**DEAL_CORNERS["largest_coverage"]))["scenarios"]["downside"]
    assert smallest["coverage_ratio"] == "-94530000000000000000000000.0000"
    # Most positive: upside cash of 51 × 1E+9 + 5% of 1E+12 = 1.01E+11 over the same payment.
    largest = {
        **dict.fromkeys(decimal_fields(DealInputs), "0"),
        "annual_revenue": "1000000000000",
        "reported_ebitda": "1000000000",
        "adjustments": [{"label": "Synthetic", "amount": "1000000000", "evidence_id": "reviewed"}]
        * ADJUSTMENTS,
        "purchase_price": "1",
        "senior_debt": TINY,
        "debt_years": 30,
    }
    upside = assert_exact(deal_inputs(**largest))["scenarios"]["upside"]
    assert upside["coverage_ratio"] == "3030000000000000000000000.0000"
    # At these sizes the fourth place still counts: a 1E-12 rate and cash with 36 places.
    for name, changes in {
        "smallest": DEAL_CORNERS["largest_coverage"] | {"debt_rate": TINY},
        "largest": largest | {"debt_rate": TINY, "tax_rate": TINY, "annual_capex": TINY},
    }.items():
        result = assert_exact(deal_inputs(**changes))
        coverage = result["scenarios"]["downside" if name == "smallest" else "upside"]["coverage_ratio"]
        assert len(coverage.lstrip("-").split(".")[0]) == (26 if name == "smallest" else 25)
        assert coverage[-4:] != "0000"
