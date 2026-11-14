"""Acquisition 1.1.0: real estate, rent, seller note schedules and investor equity.

Every expected value here is worked out by hand in the comments, or in the test with Fraction arithmetic
written out from the formulas in docs/FINANCE.md. None is taken from the model under test. The figures a
request gave on main (391b9fb) were calculated by main's own code and written in as literals.
"""

import asyncio
import itertools
import json
from fractions import Fraction
from uuid import uuid4

import httpx
import pytest

from foundry import mcp_server, models
from foundry.finance import acquisition
from foundry.models import DealInputs
from foundry.store import canonical, digest

BASE = {"target": "Synthetic target", "thesis": "A synthetic deal for the structure tests."}


def deal(**fields) -> dict:
    return acquisition(DealInputs(**(BASE | fields)), set())


def post(client, bid, body, key=None):
    return client.post(
        f"/api/businesses/{bid}/deals", headers={"Idempotency-Key": key or str(uuid4())}, json=BASE | body
    )


def memo(client, bid, deal_id):
    response = client.post(
        f"/api/businesses/{bid}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        json={"template": "investment_memo", "deal_id": deal_id},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["content"]


def amortizing(principal, rate, years) -> Fraction:
    """A level monthly payment × 12: principal × m / (1 − (1 + m)^−n) × 12, m = rate / 12, n = 12 × years."""
    p, r, n = Fraction(principal), Fraction(rate), 12 * years
    if not r:
        return p / years
    m = r / 12
    return p * m / (1 - (1 + m) ** -n) * 12


def cents(value: Fraction) -> str:
    """Half-even to cents."""
    return half_even(value, 2)


def basis(value: Fraction) -> str:
    return half_even(value, 4)


def half_even(value: Fraction, places: int) -> str:
    scaled = value * 10**places
    whole, rest = divmod(scaled.numerator, scaled.denominator)
    if 2 * rest > scaled.denominator or (2 * rest == scaled.denominator and whole % 2):
        whole += 1
    sign = "-" if whole < 0 else ""
    whole = abs(whole)
    return f"{sign}{whole // 10**places}.{whole % 10**places:0{places}d}"


def column(result, key):
    return {name: case[key] for name, case in result["scenarios"].items()}


# 1. A business and its building, priced separately, each with its own loan.

BUILDING = {
    "annual_revenue": "2000000",
    "reported_ebitda": "300000",
    "multiple_low": "3",
    "multiple_high": "5",
    "purchase_price": "900000",
    "real_estate_price": "600000",
    "fees": "40000",
    "working_capital": "60000",
    "integration_cost": "0",
    "senior_debt": "600000",
    "debt_rate": "0",
    "debt_years": 10,
    "property_debt": "480000",
    "property_rate": "0",
    "property_years": 24,
    "annual_capex": "20000",
    "annual_working_capital_growth": "0",
    "tax_rate": "0.25",
}


def test_a_business_and_its_building_are_priced_and_financed_separately():
    result = deal(**BUILDING)
    su = result["sources_and_uses"]
    # Uses: 900,000 + 600,000 + 40,000 + 60,000 + 0 = 1,600,000. Debt 600,000 + 480,000 = 1,080,000.
    assert su["purchase_price"] == "900000.00" and su["real_estate_price"] == "600000.00"
    assert su["total_uses"] == "1600000.00"
    assert su["senior_debt"] == "600000.00" and su["property_debt"] == "480000.00"
    assert su["buyer_equity_required"] == "520000.00"  # 1,600,000 − 1,080,000.
    assert su["buyer_cash_at_closing"] == "520000.00" and su["funding_beyond_uses"] == "0.00"
    # The multiples value the business only: 300,000 × 3 and × 5, with no building in them.
    assert result["enterprise_value_range"] == ["900000.00", "1500000.00"]
    # At zero rates: 600,000 / 10 = 60,000 and 480,000 / 24 = 20,000 a year, 80,000 in all.
    assert result["annual_debt_service"] == "80000.00"
    assert result["amortizing_annual_debt_service"] == "80000.00"
    # No interest, so tax is 25% of EBITDA. Base: 300,000 − 75,000 − 20,000 capex = 205,000; 205,000 −
    # 80,000 = 125,000; 205,000 / 80,000 = 2.5625. Downside: 300,000 − 10% × 2,000,000 = 100,000, tax
    # 25,000, cash 55,000, after −25,000, 0.6875. Upside: 400,000, tax 100,000, cash 280,000, 200,000, 3.5.
    assert column(result, "ebitda") == {"base": "300000.00", "downside": "100000.00", "upside": "400000.00"}
    assert column(result, "cash_available_for_debt_service") == {
        "base": "205000.00",
        "downside": "55000.00",
        "upside": "280000.00",
    }
    assert column(result, "cash_after_debt_service") == {
        "base": "125000.00",
        "downside": "-25000.00",
        "upside": "200000.00",
    }
    assert column(result, "coverage_ratio") == {"base": "2.5625", "downside": "0.6875", "upside": "3.5000"}
    assert column(result, "cash_shortfall") == {"base": False, "downside": True, "upside": False}
    assert any(
        "real estate is shown at its entered price and is not valued" in x for x in result["limitations"]
    )


def test_a_property_loan_with_interest_amortizes_and_its_interest_is_deducted_before_tax():
    result = deal(**(BUILDING | {"property_rate": "0.06", "property_years": 20}))
    # 480,000 at 6% over 20 years: m = 0.005, n = 240.
    building = amortizing("480000", "0.06", 20)
    assert cents(building) == "41266.43"  # 41,266.429 (1.005^240 = 3.3102…)
    assert result["annual_debt_service"] == cents(60000 + building)
    # Tax on EBITDA less first-year interest 480,000 × 6% = 28,800: 25% × 271,200 = 67,800, so base cash
    # available is 300,000 − 67,800 − 20,000 = 212,200.
    base = result["scenarios"]["base"]
    assert base["cash_available_for_debt_service"] == "212200.00"
    assert base["cash_after_debt_service"] == cents(212200 - 60000 - building)
    assert base["coverage_ratio"] == basis(212200 / (60000 + building))


# 2. A business bought without its building pays rent.


def test_rent_is_deducted_from_earnings_in_every_scenario():
    result = deal(
        annual_revenue="1000000",
        reported_ebitda="220000",
        purchase_price="500000",
        annual_rent="60000",
        fees="0",
        working_capital="0",
        annual_capex="0",
        tax_rate="0.2",
    )
    # Normalized EBITDA 220,000 − 60,000 rent = 160,000; valued at 3× and 5×: 480,000 – 800,000.
    assert result["normalized_ebitda"] == "160000.00"
    assert result["enterprise_value_range"] == ["480000.00", "800000.00"]
    # Base 160,000; downside 160,000 − 100,000 = 60,000; upside 160,000 + 50,000 = 210,000. No debt, so
    # cash available is 80% of each: 128,000, 48,000 and 168,000.
    assert column(result, "ebitda") == {"base": "160000.00", "downside": "60000.00", "upside": "210000.00"}
    assert column(result, "cash_available_for_debt_service") == {
        "base": "128000.00",
        "downside": "48000.00",
        "upside": "168000.00",
    }
    assert result["sources_and_uses"]["real_estate_price"] == "0.00"


# 3. The seller note on each schedule. 350,000 at 6% over 5 years, beside a senior loan of 200,000 at 0%
# over 10 years (20,000 a year), with no tax or capex, so cash available is EBITDA: base 200,000,
# downside 100,000, upside 250,000.

SELLER = {
    "annual_revenue": "1000000",
    "reported_ebitda": "200000",
    "purchase_price": "700000",
    "fees": "0",
    "working_capital": "0",
    "senior_debt": "200000",
    "debt_rate": "0",
    "debt_years": 10,
    "seller_note": "350000",
    "seller_rate": "0.06",
    "seller_years": 5,
    "annual_capex": "0",
    "tax_rate": "0",
}
AVAILABLE = {"base": 200000, "downside": 100000, "upside": 250000}
# The seller note amortizing over its 5 years: m = 0.005, n = 60, 1.005^60 = 1.34885…:
# 350,000 × 0.005 / (1 − 1 / 1.34885…) × 12 = 81,197.766… a year.
SELLER_LEVEL = amortizing("350000", "0.06", 5)


def test_an_amortizing_seller_note_pays_the_same_every_year():
    result = deal(**(SELLER | {"seller_schedule": "amortizing"}))
    assert cents(SELLER_LEVEL) == "81197.77"
    schedule = result["seller_note_schedule"]
    assert schedule == {
        "schedule": "amortizing",
        "years": 5,
        "payments": [{"year": y, "payment": "81197.77"} for y in range(1, 6)],
        "balloon": None,
    }
    assert result["annual_debt_service"] == cents(20000 + SELLER_LEVEL)
    assert column(result, "coverage_ratio") == column(result, "amortizing_coverage_ratio")


def test_an_interest_only_seller_note_pays_interest_then_the_principal():
    result = deal(**(SELLER | {"seller_schedule": "interest_only"}))
    # 350,000 × 6% = 21,000 a year; in year 5 the 350,000 as well: 371,000.
    assert result["seller_note_schedule"] == {
        "schedule": "interest_only",
        "years": 5,
        "payments": [{"year": y, "payment": "21000.00"} for y in range(1, 5)]
        + [{"year": 5, "payment": "371000.00"}],
        "balloon": {"year": 5, "amount": "350000.00", "principal": "350000.00", "accrued_interest": "0.00"},
    }
    # Year 1: 20,000 + 21,000 = 41,000. Base 200,000 / 41,000 = 4.87804…; downside 2.43902…; upside
    # 6.09756…. Cash after: 159,000, 59,000 and 209,000.
    assert result["annual_debt_service"] == "41000.00"
    assert column(result, "coverage_ratio") == {"base": "4.8780", "downside": "2.4390", "upside": "6.0976"}
    assert column(result, "cash_after_debt_service") == {
        "base": "159000.00",
        "downside": "59000.00",
        "upside": "209000.00",
    }
    # The lender's view counts the note as amortizing: 20,000 + 81,197.766… a year.
    assert result["amortizing_annual_debt_service"] == cents(20000 + SELLER_LEVEL)
    assert column(result, "amortizing_coverage_ratio") == {
        name: basis(Fraction(cash) / (20000 + SELLER_LEVEL)) for name, cash in AVAILABLE.items()
    }


def test_a_deferred_seller_note_pays_nothing_until_the_balloon_with_interest_compounded_yearly():
    result = deal(**(SELLER | {"seller_schedule": "deferred"}))
    # 350,000 × 1.06^5 = 350,000 × 1.3382255776 = 468,378.95216: 350,000 lent plus 118,378.95 of
    # interest, compounded once a year. Simple interest would give 350,000 × 1.30 = 455,000.
    assert Fraction(350000) * Fraction(106, 100) ** 5 == Fraction("468378.95216")
    assert result["seller_note_schedule"] == {
        "schedule": "deferred",
        "years": 5,
        "payments": [{"year": y, "payment": "0.00"} for y in range(1, 5)]
        + [{"year": 5, "payment": "468378.95"}],
        "balloon": {
            "year": 5,
            "amount": "468378.95",
            "principal": "350000.00",
            "accrued_interest": "118378.95",
        },
    }
    # Year 1 is due only the senior 20,000: coverage 10, 5 and 12.5; cash after 180,000, 80,000, 230,000.
    assert result["annual_debt_service"] == "20000.00"
    assert column(result, "coverage_ratio") == {"base": "10.0000", "downside": "5.0000", "upside": "12.5000"}
    assert column(result, "cash_after_debt_service") == {
        "base": "180000.00",
        "downside": "80000.00",
        "upside": "230000.00",
    }
    # Next to it, the coverage a lender may test, with the note amortizing: well below the actual one.
    lender = {name: basis(Fraction(cash) / (20000 + SELLER_LEVEL)) for name, cash in AVAILABLE.items()}
    # 200,000 / 101,197.766… = 1.97633…; 100,000 / it = 0.98816…; 250,000 / it = 2.47041….
    assert lender == {"base": "1.9763", "downside": "0.9882", "upside": "2.4704"}
    assert column(result, "amortizing_coverage_ratio") == lender
    assert result["amortizing_annual_debt_service"] == "101197.77"


def test_a_deferred_note_still_deducts_its_accrued_first_year_interest_from_tax():
    result = deal(**(SELLER | {"seller_schedule": "deferred", "tax_rate": "0.25"}))
    # Taxable: 200,000 − 350,000 × 6% = 179,000; tax 44,750; cash available 155,250.
    assert result["scenarios"]["base"]["cash_available_for_debt_service"] == "155250.00"


def test_a_one_year_note_is_due_whole_in_the_screened_year():
    deferred = deal(**(SELLER | {"seller_schedule": "deferred", "seller_years": 1}))
    # 350,000 × 1.06 = 371,000 in year 1, so the year-1 debt service is 20,000 + 371,000 = 391,000.
    assert deferred["seller_note_schedule"]["payments"] == [{"year": 1, "payment": "371000.00"}]
    assert deferred["annual_debt_service"] == "391000.00"
    assert deferred["scenarios"]["base"]["coverage_ratio"] == basis(Fraction(200000, 391000))
    interest_only = deal(**(SELLER | {"seller_schedule": "interest_only", "seller_years": 1}))
    assert interest_only["seller_note_schedule"]["payments"] == [{"year": 1, "payment": "371000.00"}]
    assert interest_only["annual_debt_service"] == "391000.00"


def test_a_deferred_note_alone_has_no_year_one_coverage_but_a_lender_one():
    result = deal(**(SELLER | {"senior_debt": "0", "seller_schedule": "deferred"}))
    assert result["annual_debt_service"] == "0.00"
    assert column(result, "coverage_ratio") == {"base": None, "downside": None, "upside": None}
    assert result["scenarios"]["base"]["amortizing_coverage_ratio"] == basis(200000 / SELLER_LEVEL)


def test_the_memo_and_the_page_explain_the_seller_note_in_plain_words(client, setup):
    _, _, _, bid, _ = setup
    saved = post(client, bid, SELLER | {"seller_schedule": "deferred"}).json()
    content = memo(client, bid, saved["id"])
    assert (
        "The seller is paid nothing until year 5, when 468,378.95 USD is due: 350,000.00 lent plus "
        "118,378.95 of interest. The year-1 coverage does not include it."
    ) in content
    assert "Debt service due in year 1, the year screened: 20,000.00 USD." in content
    assert "on that basis the annual debt service is 101,197.77 USD" in content
    header = next(line for line in content.splitlines() if line.startswith("| Case "))
    assert "Coverage if the seller note amortized" in header
    base = next(line for line in content.splitlines() if line.startswith("| Base "))
    assert [cell.strip() for cell in base.strip("|").split("|")][4:] == ["10.00x", "No", "1.98x"]
    assert "| 5    |        468,378.95 |" in content or "|    5 |        468,378.95 |" in content

    interest_only = post(client, bid, SELLER | {"seller_schedule": "interest_only"}).json()
    assert (
        "The seller is paid 21,000.00 USD of interest a year, and in year 5 the 350,000.00 lent is due as "
        "well: 371,000.00 that year."
    ) in memo(client, bid, interest_only["id"])

    level = post(client, bid, SELLER).json()
    content = memo(client, bid, level["id"])
    assert "The seller is paid 81,197.77 USD a year for 5 years" in content
    # An amortizing note has one coverage: the lender's is the same, so it has no column of its own.
    assert "Coverage if the seller note amortized" not in content


# 4. Investor equity: the buyer's cash at closing is uses less all debt and investor equity.

FUNDED = {
    "annual_revenue": "1500000",
    "reported_ebitda": "250000",
    "purchase_price": "900000",
    "fees": "50000",
    "working_capital": "50000",
    "senior_debt": "600000",
    "seller_note": "100000",
}


@pytest.mark.parametrize(
    ("investors", "cash", "beyond"),
    [("120000", "180000.00", "0.00"), ("300000", "0.00", "0.00"), ("350000", "0.00", "50000.00")],
    ids=["buyer pays", "no buyer cash", "more funding than uses"],
)
def test_investor_equity_reduces_the_buyers_cash(investors, cash, beyond):
    su = deal(**(FUNDED | {"investor_equity": investors}))["sources_and_uses"]
    # Uses 900,000 + 50,000 + 50,000 = 1,000,000; debt 700,000; equity required 300,000. Less investors:
    # 300,000 − 120,000 = 180,000; − 300,000 = 0; − 350,000 = −50,000, which is funding beyond uses.
    assert su["total_uses"] == "1000000.00" and su["buyer_equity_required"] == "300000.00"
    assert su["investor_equity"] == f"{int(investors)}.00"
    assert (su["buyer_cash_at_closing"], su["funding_beyond_uses"]) == (cash, beyond)


def test_the_memo_says_what_the_buyer_puts_in(client, setup):
    _, _, _, bid, _ = setup
    sentences = {
        "120000": "The buyer puts in 180,000.00 USD of cash at closing: total uses less all debt and "
        "investor equity.",
        "300000": "The buyer puts in no cash at closing: debt and investor equity cover every use.",
        "350000": "Funding exceeds uses by 50,000.00 USD: debt and investor equity bring in more than the "
        "deal uses, so the buyer puts in no cash at closing.",
    }
    for investors, sentence in sentences.items():
        saved = post(client, bid, FUNDED | {"investor_equity": investors})
        assert saved.status_code == 201, saved.text
        content = memo(client, bid, saved.json()["id"])
        assert sentence in content
        # A senior loan is present, so the memo names the lender rules only as something to check.
        assert "Lender rules are not applied." in content and "SOP 50 10" in content
    # With no bank loan the note is left out.
    saved = post(client, bid, {**FUNDED, "senior_debt": "0"}).json()
    assert "SOP 50 10" not in memo(client, bid, saved["id"])


# 5. A request that gives none of the new inputs: main's figures and main's fingerprint.

# Each request, its figures (every figure except the inputs and limitations) and its idempotency
# fingerprint for business "biz_synthetic", calculated by main's own code at 391b9fb and written in here.
MAIN_REQUESTS = {
    "minimal": {
        "annual_revenue": "900000",
        "reported_ebitda": "180000",
        "purchase_price": "700000",
    },
    "loans": {
        "annual_revenue": "1234567.89",
        "reported_ebitda": "234567.12",
        "adjustments": [
            {"label": "Reviewed", "amount": "12345.67", "evidence_id": "ev1"},
            {"label": "Unreviewed", "amount": "5000", "evidence_id": None},
        ],
        "replacement_management": "45000",
        "purchase_price": "700000",
        "fees": "25000.50",
        "senior_debt": "350000",
        "debt_rate": "0.085",
        "debt_years": 6,
        "seller_note": "100000",
        "seller_rate": "0.05",
        "seller_years": 4,
        "annual_capex": "12000",
        "tax_rate": "0.21",
    },
    "seller_only": {
        "annual_revenue": "600000",
        "reported_ebitda": "120000",
        "purchase_price": "400000",
        "seller_note": "150000",
    },
}
MAIN_THESES = {
    "minimal": ("Synthetic minimal", "A synthetic deal giving only required figures."),
    "loans": ("Synthetic loans", "A synthetic deal with both loans given."),
    "seller_only": ("Synthetic seller", "A synthetic deal with a seller note only."),
}
MAIN_FINGERPRINTS = {
    "minimal": "ef8565eeb1f3366dab85eada05fe26a038484dbac43ab3a59d739cfebf810948",
    "loans": "42fcb47fd72da71cef440f15a1966942f515e7f5e2c30f190b98f7542b7196c5",
    "seller_only": "346c2fd1e4557fc0f743b3d6c74970d0a4f430205e8d46c6dc2f5ca44bee2a0d",
}
MAIN_FIGURES = {
    "minimal": '{"accepted_adjustments":[],"annual_debt_service":"0.00","enterprise_value_range":["540000.00","900000.00"],"equity_value_range":["540000.00","900000.00"],"excluded_adjustments":[],"normalized_ebitda":"180000.00","scenarios":{"base":{"cash_after_debt_service":"120000.00","cash_available_for_debt_service":"120000.00","cash_shortfall":false,"coverage_ratio":null,"ebitda":"180000.00"},"downside":{"cash_after_debt_service":"52500.00","cash_available_for_debt_service":"52500.00","cash_shortfall":false,"coverage_ratio":null,"ebitda":"90000.00"},"upside":{"cash_after_debt_service":"153750.00","cash_available_for_debt_service":"153750.00","cash_shortfall":false,"coverage_ratio":null,"ebitda":"225000.00"}},"sources_and_uses":{"buyer_equity_required":"780000.00","fees":"30000.00","integration_cost":"0.00","purchase_price":"700000.00","seller_note":"0.00","senior_debt":"0.00","total_uses":"780000.00","working_capital":"50000.00"}}',
    "loans": '{"accepted_adjustments":[{"amount":"12345.67","evidence_id":"ev1","label":"Reviewed"}],"annual_debt_service":"102304.37","enterprise_value_range":["605738.37","1009563.95"],"equity_value_range":["605738.37","1009563.95"],"excluded_adjustments":[{"amount":"5000","evidence_id":null,"label":"Unreviewed","reason":"A reviewed evidence record is required."}],"normalized_ebitda":"201912.79","scenarios":{"base":{"cash_after_debt_service":"52504.24","cash_available_for_debt_service":"154808.60","cash_shortfall":false,"coverage_ratio":"1.5132","ebitda":"201912.79"},"downside":{"cash_after_debt_service":"-45026.63","cash_available_for_debt_service":"57277.74","cash_shortfall":true,"coverage_ratio":"0.5599","ebitda":"78456.00"},"upside":{"cash_after_debt_service":"101269.67","cash_available_for_debt_service":"203574.04","cash_shortfall":false,"coverage_ratio":"1.9899","ebitda":"263641.18"}},"sources_and_uses":{"buyer_equity_required":"325000.50","fees":"25000.50","integration_cost":"0.00","purchase_price":"700000.00","seller_note":"100000.00","senior_debt":"350000.00","total_uses":"775000.50","working_capital":"50000.00"}}',
    "seller_only": '{"accepted_adjustments":[],"annual_debt_service":"34799.04","enterprise_value_range":["360000.00","600000.00"],"equity_value_range":["360000.00","600000.00"],"excluded_adjustments":[],"normalized_ebitda":"120000.00","scenarios":{"base":{"cash_after_debt_service":"42450.96","cash_available_for_debt_service":"77250.00","cash_shortfall":false,"coverage_ratio":"2.2199","ebitda":"120000.00"},"downside":{"cash_after_debt_service":"-2549.04","cash_available_for_debt_service":"32250.00","cash_shortfall":true,"coverage_ratio":"0.9267","ebitda":"60000.00"},"upside":{"cash_after_debt_service":"64950.96","cash_available_for_debt_service":"99750.00","cash_shortfall":false,"coverage_ratio":"2.8665","ebitda":"150000.00"}},"sources_and_uses":{"buyer_equity_required":"330000.00","fees":"30000.00","integration_cost":"0.00","purchase_price":"400000.00","seller_note":"150000.00","senior_debt":"0.00","total_uses":"480000.00","working_capital":"50000.00"}}',
}
# The scenario figures and the sources and uses main had; 1.1.0 adds keys beside them.
MAIN_SCENARIO_KEYS = {
    "ebitda",
    "cash_available_for_debt_service",
    "cash_after_debt_service",
    "coverage_ratio",
}
MAIN_SCENARIO_KEYS |= {"cash_shortfall"}


def main_request(name):
    target, thesis = MAIN_THESES[name]
    return {"target": target, "thesis": thesis, **MAIN_REQUESTS[name]}


def figures_main_had(result):
    expected = json.loads(MAIN_FIGURES["minimal"])
    shaped = {k: result[k] for k in expected}
    shaped["sources_and_uses"] = {k: result["sources_and_uses"][k] for k in expected["sources_and_uses"]}
    shaped["scenarios"] = {
        name: {k: case[k] for k in MAIN_SCENARIO_KEYS} for name, case in result["scenarios"].items()
    }
    return json.dumps(shaped, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("name", MAIN_REQUESTS)
def test_a_request_without_the_new_inputs_gives_mains_figures_and_fingerprint(name):
    inputs = DealInputs(**main_request(name))
    result = acquisition(inputs, {"ev1"})
    assert result["model_version"] == "1.1.0"
    assert figures_main_had(result) == MAIN_FIGURES[name]
    payload = {"business_id": "biz_synthetic", **inputs.request()}
    assert digest(canonical({"operation": "deal.create", "payload": payload})) == MAIN_FINGERPRINTS[name]
    # The same request giving a new input at its example value is a different request.
    given = DealInputs(**main_request(name), annual_rent="0")
    payload = {"business_id": "biz_synthetic", **given.request()}
    assert digest(canonical({"operation": "deal.create", "payload": payload})) != MAIN_FINGERPRINTS[name]


def test_a_key_used_on_main_replays_and_a_new_input_under_it_is_refused(client, setup):
    store, _, actor, bid, _ = setup
    # Store the command as main stored it: its fingerprint, worked out from main's request, which was the
    # full dump of main's inputs plus the assumed list (src/foundry/service.py at 391b9fb).
    body = main_request("minimal")
    dump = DealInputs(**body).model_dump(mode="json")
    main_inputs = {k: v for k, v in dump.items() if k not in models.DEAL_FIELDS_ADDED_IN_1_1}
    main_assumed = [
        "replacement_management",
        "multiple_low",
        "multiple_high",
        "fees",
        "working_capital",
        "integration_cost",
        "senior_debt",
        "seller_note",
        "annual_capex",
        "annual_working_capital_growth",
        "tax_rate",
        "cash_acquired",
        "debt_assumed",
    ]
    request = {"business_id": bid, **main_inputs, "assumed_inputs": main_assumed}
    fingerprint = digest(canonical({"operation": "deal.create", "payload": request}))
    stored = {"id": "deal_from_main", "data": {"stored": "on main"}}
    with store.write() as con:
        con.execute(
            "INSERT INTO commands VALUES(?,?,?,?,?)",
            (actor["id"], "main-key", fingerprint, canonical(stored), "2026-11-01T00:00:00Z"),
        )
    replay = client.post(f"/api/businesses/{bid}/deals", headers={"Idempotency-Key": "main-key"}, json=body)
    assert replay.status_code == 201 and replay.json() == stored
    changed = client.post(
        f"/api/businesses/{bid}/deals",
        headers={"Idempotency-Key": "main-key"},
        json={**body, "investor_equity": "0"},
    )
    assert changed.status_code == 409


# 6. assumed_inputs for the new inputs, under PR #16's rules.

MINIMAL = {"annual_revenue": "900000", "reported_ebitda": "180000", "purchase_price": "700000"}
NEW_TERMS = {"property_rate", "property_years", "seller_schedule"}


def test_the_new_inputs_left_out_are_listed_and_a_loans_terms_only_with_the_loan():
    assumed = DealInputs(**BASE, **MINIMAL).assumed_inputs()
    assert [x for x in assumed if x in models.DEAL_FIELDS_ADDED_IN_1_1] == [
        "real_estate_price",
        "property_debt",
        "investor_equity",
        "annual_rent",
    ]
    with_building = DealInputs(**BASE, **MINIMAL, property_debt="300000").assumed_inputs()
    assert [x for x in with_building if x in NEW_TERMS] == ["property_rate", "property_years"]
    with_seller = DealInputs(**BASE, **MINIMAL, seller_note="100000").assumed_inputs()
    assert [x for x in with_seller if x in NEW_TERMS | {"seller_rate", "seller_years"}] == [
        "seller_rate",
        "seller_years",
        "seller_schedule",
    ]
    given = DealInputs(**BASE, **MINIMAL, seller_note="100000", seller_schedule="amortizing").assumed_inputs()
    assert "seller_schedule" not in given


def test_the_memo_names_the_new_assumed_inputs(client, setup):
    _, _, _, bid, _ = setup
    saved = post(client, bid, MINIMAL | {"property_debt": "300000", "seller_note": "100000"}).json()
    content = memo(client, bid, saved["id"])
    assert (
        "Property loan annual interest rate 8.00%; Property loan term 25 years; "
        "Seller annual interest rate 6.00%; Seller term 5 years; Seller note schedule amortizing; "
        "Investor equity 0.00; Annual rent 0.00"
    ) in content
    assert (
        saved["data"]["inputs"]["property_rate"] == "0.08" and saved["data"]["inputs"]["property_years"] == 25
    )


# 7. Refusals and cross-field checks still name the assumed values they counted.

FEES_AND_WC_ASSUMED = (
    "Debt funding exceeds transaction uses. The transaction fees and the working capital funding were "
    "not given, so their example values 30,000 and 50,000 were used; give both."
)
WC_ASSUMED = (
    "Debt funding exceeds transaction uses. The working capital funding was not given, so its example "
    "value 50,000 was used; give the working capital funding."
)
REFUSALS = [
    # Uses 700,000 + 100,000 + 30,000 + 50,000 = 880,000 against 500,000 + 380,001 of debt.
    (
        {"real_estate_price": "100000", "senior_debt": "500000", "property_debt": "380001"},
        FEES_AND_WC_ASSUMED,
    ),
    # Uses 700,000 + 50,000: the property loan alone exceeds them. The real estate price was not given,
    # but its example is zero, so it is not named.
    ({"property_debt": "750001", "fees": "0"}, WC_ASSUMED),
    (
        {"real_estate_price": "1", "property_debt": "700002", "fees": "0", "working_capital": "0"},
        "Debt funding exceeds transaction uses.",
    ),
]


@pytest.mark.parametrize(("change", "message"), REFUSALS, ids=["fees and wc", "wc", "all given"])
def test_a_debt_refusal_counts_the_property_loan_and_the_real_estate(client, setup, change, message):
    _, _, _, bid, _ = setup
    response = post(client, bid, MINIMAL | change)
    assert response.status_code == 422
    assert response.json()["detail"] == "body: Value error, " + message


def test_investor_equity_beyond_uses_is_accepted_not_refused(client, setup):
    _, _, _, bid, _ = setup
    response = post(client, bid, MINIMAL | {"fees": "0", "working_capital": "0", "investor_equity": "900000"})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["analysis"]["sources_and_uses"]["funding_beyond_uses"] == "200000.00"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("real_estate_price", "1000000000000.01"),
        ("property_debt", "-1"),
        ("property_rate", "1.000000000001"),
        ("property_years", 31),
        ("property_years", 0),
        ("investor_equity", "-0.01"),
        ("annual_rent", "1000000000001"),
        ("seller_schedule", "balloon"),
    ],
)
def test_the_new_inputs_keep_the_existing_bounds(client, setup, field, value):
    _, _, _, bid, _ = setup
    response = post(client, bid, MINIMAL | {field: value})
    assert response.status_code == 422 and f"body.{field}" in response.json()["detail"]


# 8. The MCP tool and prompt name the new inputs.


def test_the_mcp_tool_and_prompt_name_the_new_inputs(monkeypatch):
    # The server asks the API who its key belongs to; a mock transport answers as an agent, so no request
    # leaves the process. Reading a tool description and a prompt makes no other request.
    monkeypatch.setenv("FOUNDRY_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FOUNDRY_API_TOKEN", "synthetic-placeholder-not-a-key")
    real_client = httpx.Client
    agent = httpx.MockTransport(lambda request: httpx.Response(200, json={"role": "agent"}))
    monkeypatch.setattr(mcp_server.httpx, "Client", lambda **kw: real_client(transport=agent, **kw))
    server = mcp_server.build_server()

    async def read():
        tools = {t.name: t for t in await server.list_tools()}
        prompt = await server.get_prompt("acquisition_intake", {"business_id": "business_synthetic"})
        return tools["analyze_acquisition"].description, prompt.messages[0].content.text

    description, prompt = asyncio.run(read())
    for name in models.DEAL_FIELDS_ADDED_IN_1_1:
        assert name in description, name
    for words in ("real estate", "rent", "investor", "interest only", "deferred"):
        assert words in prompt, words
    # Buyer cash at closing is floored at zero, and the excess has its own figure.
    assert (
        "buyer_cash_at_closing is uses less all debt and investor equity, and never below zero: when debt and "
        "investor equity bring in more than the uses, it is 0.00 and funding_beyond_uses is the excess."
    ) in description


# 9. A deal saved under acquisition 1.0.1 keeps rendering as it did.


def test_a_memo_for_a_deal_saved_under_1_0_1_shows_only_the_figures_it_has(setup):
    from test_deal_inputs import MAIN_SENT, as_main, main_dump, save_deal_as_before

    from foundry.documents import render_document

    _, service, actor, bid, _ = setup
    old = save_deal_as_before(service, actor, bid, str(uuid4()), main_dump(MAIN_SENT))
    assert old["data"]["analysis"]["model_version"] == "1.0.1"
    assert as_main(old["data"]["analysis"]) == old["data"]["analysis"]
    snapshot = service.snapshot(actor, bid)
    deal_object = next(o for o in snapshot["objects"] if o["id"] == old["id"])
    _, content = render_document("investment_memo", snapshot, deal_object)
    # main's rows and debt line, and none of 1.1.0's.
    row = next(line for line in content.splitlines() if "Buyer equity required" in line)
    assert [cell.strip() for cell in row.strip("|").split("|")] == [
        "Sources",
        "Buyer equity required",
        "330,000.50",
    ]
    assert "Annual debt service: 102,304.37 USD." in content
    for added in (
        "Real estate",
        "Property loan",
        "Investor equity",
        "Buyer cash at closing",
        "Less: annual rent",
        "Seller note payments",
        "Coverage if the seller note amortized",
        "Lender rules",
        "year 1",
    ):
        assert added not in content, added


# 10. What the memo says about amounts that round away. The figures are the model's, unchanged; only the
# sentences about them are tested here. The seller notes are tiny so that a sub-cent amount decides the words.

TINY = SELLER | {"senior_debt": "0"}


def stored(service, actor, bid, deal_id):
    return next(o for o in service.snapshot(actor, bid)["objects"] if o["id"] == deal_id)


def test_a_payment_below_half_a_cent_is_still_due_in_year_one(client, setup):
    _, service, actor, bid, _ = setup
    body = TINY | {"seller_note": "0.001", "seller_rate": "0", "seller_years": 1}
    # 0.001 at zero interest over 1 year: 0.001 is due in year 1, which shows as 0.00. Cash available is
    # EBITDA (no tax, capex or interest), so the base coverage is 200,000 / 0.001 = 200,000,000.
    result = deal(**body)
    assert result["annual_debt_service"] == "0.00"
    assert column(result, "coverage_ratio") == {
        "base": "200000000.0000",
        "downside": "100000000.0000",
        "upside": "250000000.0000",
    }
    saved = post(client, bid, body).json()
    before = stored(service, actor, bid, saved["id"])
    content = memo(client, bid, saved["id"])
    assert "Debt service due in year 1, the year screened: 0.00 USD." in content
    assert "Nothing is due" not in content
    assert "no year-1 coverage ratio" not in content
    assert stored(service, actor, bid, saved["id"]) == before


@pytest.mark.parametrize(
    "body",
    [
        # A deferred note over 5 years is due nothing in year 1.
        TINY | {"seller_schedule": "deferred"},
        # An interest-only note at zero interest over 3 years is due nothing in year 1.
        TINY | {"seller_schedule": "interest_only", "seller_rate": "0", "seller_years": 3},
        # The same note of only 0.001.
        TINY | {"seller_schedule": "deferred", "seller_note": "0.001"},
    ],
    ids=["deferred", "interest_only_at_zero", "tiny_deferred"],
)
def test_a_note_with_nothing_due_in_year_one_says_so(client, setup, body):
    _, _, _, bid, _ = setup
    assert column(deal(**body), "coverage_ratio") == {"base": None, "downside": None, "upside": None}
    saved = post(client, bid, body).json()
    assert (
        "Debt service due in year 1, the year screened: 0.00 USD. Nothing is due on any loan in year 1, "
        "so there is no year-1 coverage ratio."
    ) in memo(client, bid, saved["id"])


def test_nothing_due_in_year_one_is_said_exactly_when_the_model_gives_no_coverage():
    """The memo's words agree with the model's own test on the exact debt service, for every kind of loan,
    with amounts above and below half a cent."""
    from foundry.documents import nothing_due_in_year_one

    loans = itertools.product(
        ("0", "0.001"),
        ("0", "0.001"),
        ("0", "0.001", "1000"),
        ("0", "0.05"),
        (1, 3),
        ("amortizing", "interest_only", "deferred"),
    )
    for senior, building, note, rate, years, schedule in loans:
        result = deal(
            **(
                SELLER
                | {
                    "senior_debt": senior,
                    "property_debt": building,
                    "seller_note": note,
                    "seller_rate": rate,
                    "seller_years": years,
                    "seller_schedule": schedule,
                }
            )
        )
        no_coverage = result["scenarios"]["base"]["coverage_ratio"] is None
        assert nothing_due_in_year_one(result["inputs"]) == no_coverage, result["inputs"]


@pytest.mark.parametrize(
    "body, schedule, sentence",
    [
        # Interest only, one year: 1.005 + 1.005 × 0.01 = 1.01505 is due, shown as 1.02; the principal
        # 1.005 shows as 1.00 (half-even); the interest 0.01005 shows as 0.01. The rounded amounts
        # 1.02 − 1.00 would say 0.02.
        (
            {"seller_schedule": "interest_only", "seller_years": 1},
            {
                "schedule": "interest_only",
                "years": 1,
                "payments": [{"year": 1, "payment": "1.02"}],
                "balloon": {"year": 1, "amount": "1.00", "principal": "1.00", "accrued_interest": "0.00"},
            },
            "The seller is paid once, at the end of year 1: 1.02 USD, the 1.00 lent plus 0.01 of interest.",
        ),
        # Interest only, three years: 0.01005 a year, shown as 0.01; in year 3, 1.01505, shown as 1.02.
        # The rounded amounts 1.02 − 1.00 would say 0.02 a year.
        (
            {"seller_schedule": "interest_only", "seller_years": 3},
            {
                "schedule": "interest_only",
                "years": 3,
                "payments": [
                    {"year": 1, "payment": "0.01"},
                    {"year": 2, "payment": "0.01"},
                    {"year": 3, "payment": "1.02"},
                ],
                "balloon": {"year": 3, "amount": "1.00", "principal": "1.00", "accrued_interest": "0.00"},
            },
            "The seller is paid 0.01 USD of interest a year, and in year 3 the 1.00 lent is due as well: "
            "1.02 that year. The year-1 coverage does not include the 1.00.",
        ),
        # Deferred, two years: 1.005 × 1.01² = 1.005 × 1.0201 = 1.0252005, shown as 1.03; the accrued
        # interest 0.0202005 shows as 0.02. The rounded amounts 1.03 − 1.00 would say 0.03.
        (
            {"seller_schedule": "deferred", "seller_years": 2},
            {
                "schedule": "deferred",
                "years": 2,
                "payments": [{"year": 1, "payment": "0.00"}, {"year": 2, "payment": "1.03"}],
                "balloon": {"year": 2, "amount": "1.03", "principal": "1.00", "accrued_interest": "0.02"},
            },
            "The seller is paid nothing until year 2, when 1.03 USD is due: 1.00 lent plus 0.02 of interest. "
            "The year-1 coverage does not include it.",
        ),
    ],
    ids=["interest_only_one_year", "interest_only_three_years", "deferred_two_years"],
)
def test_the_interest_is_rounded_once_not_taken_from_rounded_amounts(client, setup, body, schedule, sentence):
    _, service, actor, bid, _ = setup
    body = TINY | {"seller_note": "1.005", "seller_rate": "0.01"} | body
    saved = post(client, bid, body).json()
    # The stored figures are the model's, as they were before the memo's wording changed.
    assert saved["data"]["analysis"]["seller_note_schedule"] == schedule
    before = stored(service, actor, bid, saved["id"])
    assert sentence in memo(client, bid, saved["id"])
    assert stored(service, actor, bid, saved["id"]) == before
