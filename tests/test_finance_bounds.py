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
from pydantic import ValidationError

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


def test_money_fields_refuse_digits_beyond_twelve_places():
    amount = "123.456789012345678901234567890123456789012"
    for build in (lambda x: ForecastInputs(unit_price=x), lambda x: DealInputs(**DEAL, fees=x)):
        with pytest.raises(ValidationError) as caught:
            build(amount)
        assert caught.value.errors()[0]["type"] == "decimal_max_places"
    assert ForecastInputs(unit_price=amount[:16]).unit_price == D("123.456789012345")
    assert DealInputs(**DEAL, fees=amount[:16]).fees == D("123.456789012345")


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


def decimal_fields(model):
    return [
        name
        for name, field in model.model_fields.items()
        if Decimal in (field.annotation, *typing.get_args(field.annotation))
    ]


def field_case(model, field, value):
    """A body that is valid apart from `field`, the model that validates it, and the field's location."""
    if model is ForecastInputs:
        inputs = {} if field == "terminal_growth" else {"terminal_growth": "0"}
        if field == "fixed_cost_step":
            inputs["fixed_cost_step_month"] = 1
        return {**inputs, field: value}, (field,)
    if model is Adjustment:
        return {**DEAL, "adjustments": [{"label": "Synthetic", field: value}]}, ("adjustments", 0, field)
    body = {**DEAL, field: value}
    if field == "multiple_high":
        body["multiple_low"] = "0"
    if field == "annual_revenue":
        body["reported_ebitda"] = "0"
    if field == "purchase_price":
        body["senior_debt"] = "0"
    return body, (field,)


DECIMAL_FIELDS = [(m, f) for m in (ForecastInputs, DealInputs, Adjustment) for f in decimal_fields(m)]


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
        client, bid, "deals", {"target": "Synthetic", "thesis": "Synthetic testing", "senior_debt": zero}
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
        deal = DealInputs(
            **DEAL,
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
        client, bid, "deals", {"target": "Synthetic", "thesis": "Synthetic testing", "senior_debt": principal}
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
        {"target": "Synthetic", "thesis": "Synthetic testing", "senior_debt": "1E-12"},
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
# the models compute in the caller's context stays within 1E-100 <= |v| < 1E+46 (docs/FINANCE.md). The exact
# debt-service fraction uses its own contexts and is checked against exact rationals instead.

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
    inputs = DealInputs(**DEAL, **DEAL_CORNERS[corner])
    with bounded_magnitudes():
        result = acquisition(inputs, {"reviewed"})
    values = displayed(result)
    assert values and all(abs(v) < 10**46 for v in values) and no_negative_zero(result)
    loans = [(inputs.senior_debt, inputs.debt_rate, inputs.debt_years)]
    loans.append((inputs.seller_note, inputs.seller_rate, inputs.seller_years))
    assert result["annual_debt_service"] == half_even(exact_total(*loans))


def test_corner_outputs_match_the_stated_extremes():
    # The largest coverage ratio: cash of about −3.15E+12 over the smallest service, 1E-12 / 30 years.
    deal = acquisition(DealInputs(**DEAL, **DEAL_CORNERS["largest_coverage"]), {"reviewed"})
    coverage = Fraction(D(deal["scenarios"]["downside"]["coverage_ratio"]))
    assert -(10**26) < coverage < -(10**25)
    # The largest money output: the enterprise value with the narrowest spread, below 1E+44.
    plan = forecast(ForecastInputs(**EXTREME))["scenarios"]["upside"]["summary"]["dcf"]
    assert 10**42 < Fraction(D(plan["enterprise_value"])) < 10**44
    # The smallest non-zero outputs are one cent and one basis point.
    tiny = acquisition(DealInputs(**DEAL, **DEAL_CORNERS["smallest"]), {"reviewed"})
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
        **DealInputs(**DEAL).model_dump(mode="json"),
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
