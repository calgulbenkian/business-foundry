"""An acquisition analysis never runs on figures nobody gave.

The target's revenue and reported EBITDA and the purchase price are required. Every other figure the
caller leaves out takes the model's example value, and the deal records it in assumed_inputs, the same
way through the API, the command line and the MCP adapter. Expected values are written out by hand.
"""

import ast
import asyncio
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from foundry import catalog, cli, finance, models
from foundry.api import create_app
from foundry.demo import seed
from foundry.documents import render_document
from foundry.finance import acquisition
from foundry.models import DealInputs
from foundry.service import Service
from foundry.store import Store

# Every figure main (391b9fb) had, each different from its example value.
MAIN_FULL = {
    "target": "Synthetic full target",
    "thesis": "A synthetic deal that gives every input.",
    "annual_revenue": "1234567.89",
    "reported_ebitda": "234567.12",
    "adjustments": [
        {"label": "Reviewed", "amount": "12345.67", "evidence_id": "ev1"},
        {"label": "Unreviewed", "amount": "5000", "evidence_id": None},
    ],
    "replacement_management": "45000",
    "multiple_low": "2.5",
    "multiple_high": "4.25",
    "purchase_price": "700000",
    "fees": "25000.50",
    "working_capital": "40000",
    "integration_cost": "15000",
    "senior_debt": "350000",
    "debt_rate": "0.085",
    "debt_years": 6,
    "seller_note": "100000",
    "seller_rate": "0.05",
    "seller_years": 4,
    "annual_capex": "12000",
    "annual_working_capital_growth": "3000",
    "tax_rate": "0.21",
    "cash_acquired": "20000",
    "debt_assumed": "10000",
    "evidence_ids": [],
}
# The inputs acquisition 1.1.0 added, each different from its example value.
STRUCTURE = {
    "real_estate_price": "250000",
    "property_debt": "200000",
    "property_rate": "0.07",
    "property_years": 20,
    "seller_schedule": "interest_only",
    "investor_equity": "50000",
    "annual_rent": "12000",
}
# Every figure, each different from its example value.
FULL = {**MAIN_FULL, **STRUCTURE}
# The same deals with no evidence to look up, for requests that go through the service.
UNREVIEWED = [{"label": "Unreviewed", "amount": "5000", "evidence_id": None}]
SENT = {**FULL, "adjustments": UNREVIEWED}
MAIN_SENT = {**MAIN_FULL, "adjustments": UNREVIEWED}
MINIMAL = {
    "target": "Synthetic minimal target",
    "thesis": "A synthetic deal that gives only the required figures.",
    "annual_revenue": "900000",
    "reported_ebitda": "180000",
    "purchase_price": "700000",
}
# Every figure with an example value that the model used, in the field order of DealInputs. With no loan
# given, every loan is zero, so their rates and terms, and the seller note's schedule, are not used and
# not listed.
MINIMAL_ASSUMED = [
    "replacement_management",
    "multiple_low",
    "multiple_high",
    "real_estate_price",
    "fees",
    "working_capital",
    "integration_cost",
    "senior_debt",
    "property_debt",
    "seller_note",
    "investor_equity",
    "annual_rent",
    "annual_capex",
    "annual_working_capital_growth",
    "tax_rate",
    "cash_acquired",
    "debt_assumed",
]
MINIMAL_SENTENCE = (
    "Not given, so the model's example value was used: Annual replacement management cost 0.00; "
    "Low EBITDA multiple 3.00x; High EBITDA multiple 5.00x; Real estate price 0.00; "
    "Transaction fees 30,000.00; Working capital funding 50,000.00; Integration costs 0.00; "
    "New senior debt 0.00; Property loan 0.00; Seller financing 0.00; Investor equity 0.00; "
    "Annual rent 0.00; Annual maintenance capex 15,000.00; "
    "Annual working capital increase 0.00; Assumed tax rate 25.00%; Cash in valuation equity bridge 0.00; "
    "Debt in valuation equity bridge 0.00."
)
NOT_RECORDED = (
    "Not recorded. This analysis was saved before Foundry recorded which inputs were given, "
    "so any of its figures may be the model's example values. Check each one."
)
REQUIRED = ["annual_revenue", "reported_ebitda", "purchase_price"]


def post(client, bid, body, key=None):
    return client.post(
        f"/api/businesses/{bid}/deals", headers={"Idempotency-Key": key or str(uuid4())}, json=body
    )


def counts(store):
    with store.read() as con:
        return {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("objects", "events", "commands")
        }


def kinds(store, kind):
    with store.read() as con:
        return con.execute("SELECT COUNT(*) FROM objects WHERE kind=?", (kind,)).fetchone()[0]


def without(body, *fields):
    return {k: v for k, v in body.items() if k not in fields}


def memo(client, bid, deal_id):
    response = client.post(
        f"/api/businesses/{bid}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        json={"template": "investment_memo", "deal_id": deal_id},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["content"]


def inputs_section(content):
    """The paragraph under the memo's "Inputs not given" heading."""
    lines = content.splitlines()
    start = lines.index("### Inputs not given")
    assert lines[start + 1] == "" and lines[start + 3] == ""
    return lines[start + 2]


# 1. The three figures that describe the target and its price are required.


@pytest.mark.parametrize(
    "missing",
    [["annual_revenue"], ["reported_ebitda"], ["purchase_price"], REQUIRED],
    ids=["revenue", "ebitda", "price", "all three"],
)
def test_a_deal_without_a_required_figure_is_refused_and_stores_nothing(client, setup, missing):
    store, _, _, bid, _ = setup
    before, tasks = counts(store), kinds(store, "task")
    response = post(client, bid, without(SENT, *missing), key="deal-key")
    assert response.status_code == 422
    assert response.json()["detail"] == "; ".join(f"body.{field}: Field required" for field in missing)
    assert counts(store) == before and kinds(store, "task") == tasks and kinds(store, "deal") == 0

    # The key was not used up: the same key then saves the full request.
    response = post(client, bid, SENT, key="deal-key")
    assert response.status_code == 201
    assert response.json()["data"]["assumed_inputs"] == []
    assert kinds(store, "deal") == 1 and kinds(store, "task") == tasks + len(catalog.DEAL_TASKS)


def test_cli_refuses_a_configuration_without_a_required_figure(tmp_path, capsys):
    db = str(tmp_path / "cli" / "foundry.db")
    cli.main(["--db", db, "init"])
    business = tmp_path / "business.json"
    business.write_text(json.dumps({"name": "CLI deal", "concept": "A synthetic business for a CLI deal."}))
    capsys.readouterr()
    cli.main(["--db", db, "create", str(business)])
    bid = json.loads(capsys.readouterr().out)["id"]
    store = Store(db)
    before = counts(store)
    deal = tmp_path / "deal.json"

    deal.write_text(json.dumps(without(SENT, "reported_ebitda")))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--db", db, "acquire", bid, str(deal)])
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Foundry: deal.json was not accepted: reported_ebitda is required.\n"

    deal.write_text(json.dumps(without(SENT, *REQUIRED)))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--db", db, "acquire", bid, str(deal)])
    assert stopped.value.code == 1
    assert capsys.readouterr().err == (
        "Foundry: deal.json was not accepted: annual_revenue is required; reported_ebitda is required; "
        "purchase_price is required.\n"
    )
    assert counts(store) == before and kinds(store, "deal") == 0

    # The command line records the same list as the API, for a minimal and a full configuration.
    deal.write_text(json.dumps(MINIMAL))
    cli.main(["--db", db, "acquire", bid, str(deal)])
    assert json.loads(capsys.readouterr().out)["data"]["assumed_inputs"] == MINIMAL_ASSUMED
    deal.write_text(json.dumps(SENT))
    cli.main(["--db", db, "acquire", bid, str(deal)])
    assert json.loads(capsys.readouterr().out)["data"]["assumed_inputs"] == []


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


def test_mcp_tool_passes_back_the_refusal_and_records_assumed_inputs(setup):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(owner, {"name": "Deal agent", "role": "agent", "business_ids": [bid]})
    seen = {}

    async def check(url):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={**os.environ, "FOUNDRY_API_URL": url, "FOUNDRY_API_TOKEN": agent["token"]},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tool = next(t for t in (await client.list_tools()).tools if t.name == "analyze_acquisition")
                seen["description"] = tool.description
                before = counts(store)
                result = await client.call_tool(
                    "analyze_acquisition",
                    {
                        "business_id": bid,
                        "command_key": "mcp-deal",
                        "inputs": without(MINIMAL, "purchase_price"),
                    },
                )
                assert result.isError
                assert "body.purchase_price: Field required" in result.content[0].text
                assert counts(store) == before and kinds(store, "deal") == 0
                result = await client.call_tool(
                    "analyze_acquisition", {"business_id": bid, "command_key": "mcp-deal", "inputs": MINIMAL}
                )
                assert not result.isError
                seen["deal"] = json.loads(result.content[0].text)

    with api_server(store) as url:
        asyncio.run(check(url))
    assert seen["deal"]["data"]["assumed_inputs"] == MINIMAL_ASSUMED
    description = " ".join(seen["description"].split())
    assert "target, thesis, annual_revenue, reported_ebitda and purchase_price are required" in description
    assert "data.assumed_inputs" in description
    assert kinds(store, "deal") == 1


# 2. Every other input the caller did not give is recorded as assumed, and shown.


def test_assumed_inputs_name_exactly_the_figures_left_out(client, setup):
    _, _, _, bid, _ = setup
    full = post(client, bid, SENT).json()
    assert full["data"]["assumed_inputs"] == []
    minimal = post(client, bid, MINIMAL).json()
    assert minimal["data"]["assumed_inputs"] == MINIMAL_ASSUMED
    two = post(client, bid, without(SENT, "fees", "seller_years")).json()
    assert two["data"]["assumed_inputs"] == ["fees", "seller_years"]
    # The stored inputs still hold every value the model used.
    assert minimal["data"]["inputs"]["fees"] == "30000" and minimal["data"]["inputs"]["debt_years"] == 7
    assert two["data"]["inputs"]["fees"] == "30000" and two["data"]["inputs"]["seller_years"] == 5
    assert two["data"]["analysis"]["sources_and_uses"]["fees"] == "30000.00"

    stored = {
        o["id"]: o for o in client.get(f"/api/businesses/{bid}").json()["objects"] if o["kind"] == "deal"
    }
    assert stored[full["id"]]["data"]["assumed_inputs"] == []
    assert stored[minimal["id"]]["data"]["assumed_inputs"] == MINIMAL_ASSUMED
    assert stored[two["id"]]["data"]["assumed_inputs"] == ["fees", "seller_years"]


def test_a_figure_sent_with_its_example_value_is_the_callers_own(client, setup):
    _, _, _, bid, _ = setup
    body = {**MINIMAL, "fees": "30000", "debt_years": 7, "tax_rate": "0.25", "seller_note": "0"}
    deal = post(client, bid, body).json()
    assert deal["data"]["assumed_inputs"] == [
        "replacement_management",
        "multiple_low",
        "multiple_high",
        "real_estate_price",
        "working_capital",
        "integration_cost",
        "senior_debt",
        "property_debt",
        "investor_equity",
        "annual_rent",
        "annual_capex",
        "annual_working_capital_growth",
        "cash_acquired",
        "debt_assumed",
    ]


def test_a_replay_returns_the_same_deal_and_list(client, setup):
    store, _, _, bid, _ = setup
    first = post(client, bid, MINIMAL, key="replay-key")
    after = counts(store)
    again = post(client, bid, MINIMAL, key="replay-key")
    assert first.status_code == 201 and again.status_code == 201
    assert again.json() == first.json()
    assert again.json()["data"]["assumed_inputs"] == MINIMAL_ASSUMED
    assert counts(store) == after
    # Giving a figure the first request left out is a different request, even with the same value.
    changed = post(client, bid, {**MINIMAL, "fees": "30000"}, key="replay-key")
    assert changed.status_code == 409
    assert counts(store) == after


def test_the_memo_lists_the_assumed_inputs_with_their_values(client, setup):
    _, service, actor, _, _ = setup
    business = service.create_business(
        actor,
        str(uuid4()),
        models.BusinessCreate(
            name="Krona shop", concept="A synthetic business that keeps its books in kronor.", currency="SEK"
        ).model_dump(mode="json"),
    )
    bid = business["id"]
    minimal = post(client, bid, MINIMAL).json()
    content = memo(client, bid, minimal["id"])
    assert "Currency: SEK." in content
    assert inputs_section(content) == MINIMAL_SENTENCE
    two = post(client, bid, without(SENT, "fees", "tax_rate")).json()
    assert inputs_section(memo(client, bid, two["id"])) == (
        "Not given, so the model's example value was used: Transaction fees 30,000.00; Assumed tax rate 25.00%."
    )
    full = post(client, bid, SENT).json()
    assert inputs_section(memo(client, bid, full["id"])) == (
        "Every input the calculation used was given. The model used none of its example values."
    )


def test_an_empty_list_claims_only_the_inputs_the_calculation_used(client, setup):
    """With every loan at zero and their rates, terms and schedule left out, nothing assumed entered a
    figure, so the list is empty. Those inputs were not given, so the memo does not say every input was."""
    _, _, _, bid, _ = setup
    terms = ("debt_rate", "debt_years", "property_rate", "property_years", "seller_rate", "seller_years")
    body = without(SENT, *terms, "seller_schedule") | {
        "senior_debt": "0",
        "property_debt": "0",
        "seller_note": "0",
    }
    saved = post(client, bid, body).json()
    assert saved["data"]["assumed_inputs"] == []
    content = memo(client, bid, saved["id"])
    assert inputs_section(content) == (
        "Every input the calculation used was given. The model used none of its example values."
    )
    assert "Every input was given." not in content


# 3. Deals stored before this change still load.


def main_dump(body):
    """DealInputs dumped as main dumped it: every input main had, with its defaults filled in."""
    dump = DealInputs(**body).model_dump(mode="json")
    return {k: v for k, v in dump.items() if k not in models.DEAL_FIELDS_ADDED_IN_1_1}


def as_main(result):
    """An analysis as acquisition 1.0.1 stored it: the figures it had, under its version.

    test_figures_for_a_fixed_full_payload_are_unchanged_from_main shows that each figure 1.0.1 had is
    unchanged in 1.1.0, so dropping the figures 1.1.0 added and its limitations gives 1.0.1's analysis.
    """
    result = {k: v for k, v in result.items() if k not in ADDED_FIGURES}
    result["sources_and_uses"] = {
        k: v for k, v in result["sources_and_uses"].items() if k not in ADDED_SOURCES_AND_USES
    }
    result["scenarios"] = {
        name: {k: v for k, v in case.items() if k != "amortizing_coverage_ratio"}
        for name, case in result["scenarios"].items()
    }
    return {**result, "model_version": "1.0.1", "limitations": MAIN_LIMITATIONS}


def save_deal_as_before(service, actor, bid, key, payload):
    """save_deal as it was before assumed_inputs existed (e2317d3): the payload was the full dump, and the
    analysis was acquisition 1.0.1's."""

    def run(con):
        ids = payload["evidence_ids"] + [
            a["evidence_id"] for a in payload["adjustments"] if a.get("evidence_id")
        ]
        evidence = service.references(con, bid, ids, "evidence")
        reviewed = {e["id"] for e in evidence if e["state"] == "verified"}
        result = as_main(finance.acquisition(DealInputs.model_validate(payload), reviewed))
        data = {
            "inputs": payload,
            "analysis": result,
            "evidence_ids": list(set(ids)),
            "evidence_versions": {e["id"]: e["version"] for e in evidence},
        }
        item = service.store.add(con, bid, "deal", payload["target"], data, actor, "screening")
        service._tasks(con, bid, catalog.DEAL_TASKS, actor, item["id"])
        return item

    return service.command(actor, bid, key, "deal.create", payload, run)


def test_a_deal_stored_without_the_list_loads_and_says_not_recorded(client, setup):
    store, service, actor, bid, _ = setup
    old = save_deal_as_before(service, actor, bid, str(uuid4()), main_dump(MAIN_SENT))
    old_fields = {"inputs", "analysis", "evidence_ids", "evidence_versions"}

    snapshot = client.get(f"/api/businesses/{bid}").json()
    deal = next(o for o in snapshot["objects"] if o["id"] == old["id"])
    assert set(deal["data"]) == old_fields
    assert deal["data"]["analysis"]["normalized_ebitda"] == "189567.12"
    assert inputs_section(memo(client, bid, old["id"])) == NOT_RECORDED
    assert service.audit(actor, bid)["integrity"]["valid"]

    # A jurisdiction change still marks it, read from the events, with nothing rewritten.
    version = service.snapshot(actor, bid)["business"]["version"]
    service.change_jurisdiction(
        actor,
        bid,
        str(uuid4()),
        models.JurisdictionChange(
            expected_version=version, jurisdiction="GB", reason="Synthetic move for a test."
        ).model_dump(mode="json"),
    )
    deal = next(o for o in service.snapshot(actor, bid)["objects"] if o["id"] == old["id"])
    assert deal["stale"] is True and [r["code"] for r in deal["stale_reasons"]] == ["jurisdiction_changed"]
    with store.read() as con:
        row = con.execute("SELECT version, data FROM objects WHERE id=?", (old["id"],)).fetchone()
    assert row["version"] == 1 and set(json.loads(row["data"])) == old_fields
    assert service.audit(actor, bid)["integrity"]["valid"]


def test_the_memo_renders_an_old_deal_from_a_snapshot(setup):
    _, service, actor, bid, _ = setup
    old = save_deal_as_before(service, actor, bid, str(uuid4()), main_dump(MAIN_SENT))
    snapshot = service.snapshot(actor, bid)
    deal = next(o for o in snapshot["objects"] if o["id"] == old["id"])
    _, content = render_document("investment_memo", snapshot, deal)
    assert inputs_section(content) == NOT_RECORDED


# 7. The arithmetic is unchanged: figures computed on main (e2317d3) for a fixed full payload and the demo.
# Acquisition 1.1.0 adds figures; every figure 1.0.1 had is the same for a request that gives none of the
# inputs 1.1.0 added.

# The figures acquisition 1.1.0 added, at the top level and in sources and uses.
ADDED_FIGURES = ("debt_service_year", "amortizing_annual_debt_service", "seller_note_schedule")
ADDED_SOURCES_AND_USES = (
    "real_estate_price",
    "property_debt",
    "investor_equity",
    "buyer_cash_at_closing",
    "funding_beyond_uses",
)
# Acquisition 1.0.1's limitations, copied from src/foundry/finance.py at 391b9fb.
MAIN_LIMITATIONS = [
    "Multiples are user assumptions, not licensed transaction comparables.",
    "Purchase price is the negotiated cash-equivalent consideration; existing claims must be explicitly included in transaction uses.",
    "Debt service is level monthly amortization, annualized. Tax uses an approximate first-year interest estimate and excludes depreciation.",
    "Downside subtracts 10% of revenue from EBITDA with no cost relief. Upside adds 5% of revenue; synergies are excluded.",
    "Evidence review records a human attestation; it is not an audit or a quality-of-earnings opinion.",
]


def figures(analysis):
    return {k: v for k, v in analysis.items() if k not in {"inputs", "limitations"}}


def main_figures(analysis):
    """The figures acquisition 1.0.1 had, from a 1.1.0 analysis, with its model version."""
    shaped = as_main(analysis)
    return {**figures(shaped), "model_version": analysis["model_version"]}


def added_figures(analysis):
    """The figures acquisition 1.1.0 added."""
    return {
        **{k: analysis[k] for k in ADDED_FIGURES},
        "sources_and_uses": {k: analysis["sources_and_uses"][k] for k in ADDED_SOURCES_AND_USES},
        "amortizing_coverage_ratio": {
            name: case["amortizing_coverage_ratio"] for name, case in analysis["scenarios"].items()
        },
    }


def test_figures_for_a_fixed_full_payload_are_unchanged_from_main():
    result = acquisition(DealInputs(**MAIN_FULL), {"ev1"})
    assert main_figures(result) == {
        "model_version": "1.1.0",
        "normalized_ebitda": "201912.79",
        "accepted_adjustments": [{"label": "Reviewed", "amount": "12345.67", "evidence_id": "ev1"}],
        "excluded_adjustments": [
            {
                "label": "Unreviewed",
                "amount": "5000",
                "evidence_id": None,
                "reason": "A reviewed evidence record is required.",
            }
        ],
        "enterprise_value_range": ["504781.98", "858129.36"],
        "equity_value_range": ["514781.98", "868129.36"],
        "sources_and_uses": {
            "purchase_price": "700000.00",
            "fees": "25000.50",
            "working_capital": "40000.00",
            "integration_cost": "15000.00",
            "total_uses": "780000.50",
            "senior_debt": "350000.00",
            "seller_note": "100000.00",
            "buyer_equity_required": "330000.50",
        },
        "annual_debt_service": "102304.37",
        "scenarios": {
            "base": {
                "ebitda": "201912.79",
                "cash_available_for_debt_service": "151808.60",
                "cash_after_debt_service": "49504.24",
                "coverage_ratio": "1.4839",
                "cash_shortfall": False,
            },
            "downside": {
                "ebitda": "78456.00",
                "cash_available_for_debt_service": "54277.74",
                "cash_after_debt_service": "-48026.63",
                "coverage_ratio": "0.5306",
                "cash_shortfall": True,
            },
            "upside": {
                "ebitda": "263641.18",
                "cash_available_for_debt_service": "200574.04",
                "cash_after_debt_service": "98269.67",
                "coverage_ratio": "1.9606",
                "cash_shortfall": False,
            },
        },
    }
    # The seller note of 100,000 at 5% over 4 years amortizes: monthly rate m = 0.05 / 12 over 48 months,
    # payment = 100,000 × m / (1 − (1 + m)^−48) = 2,302.929… a month, × 12 = 27,635.15 a year, written
    # out in rationals here rather than taken from the model.
    m = Fraction(5, 1200)
    seller_payment = 100000 * m / (1 - (1 + m) ** -48) * 12
    assert round(seller_payment, 2) == Fraction("27635.15")  # Fraction rounds half-even, exactly.
    assert added_figures(result) == {
        "debt_service_year": 1,
        # The seller note amortizes, so the lender's view is the same debt service and coverage.
        "amortizing_annual_debt_service": "102304.37",
        "seller_note_schedule": {
            "schedule": "amortizing",
            "years": 4,
            "payments": [{"year": y, "payment": "27635.15"} for y in (1, 2, 3, 4)],
            "balloon": None,
        },
        # No building, no property loan, no investors: the buyer's cash is the equity required.
        "sources_and_uses": {
            "real_estate_price": "0.00",
            "property_debt": "0.00",
            "investor_equity": "0.00",
            "buyer_cash_at_closing": "330000.50",
            "funding_beyond_uses": "0.00",
        },
        "amortizing_coverage_ratio": {"base": "1.4839", "downside": "0.5306", "upside": "1.9606"},
    }


def test_the_demo_deal_has_the_same_figures_as_on_main(setup):
    _, service, actor, _, _ = setup
    business = seed(service, actor)
    deal = next(o for o in service.snapshot(actor, business["id"])["objects"] if o["kind"] == "deal")
    analysis = main_figures(deal["data"]["analysis"])
    for item in analysis["accepted_adjustments"]:
        item["evidence_id"] = "reviewed"
    assert analysis == {
        "model_version": "1.1.0",
        "normalized_ebitda": "150000.00",
        "accepted_adjustments": [
            {"label": "Illustrative one-time expense", "amount": "10000", "evidence_id": "reviewed"}
        ],
        "excluded_adjustments": [
            {
                "label": "Unsupported seller add-back",
                "amount": "25000",
                "evidence_id": None,
                "reason": "A reviewed evidence record is required.",
            }
        ],
        "enterprise_value_range": ["450000.00", "750000.00"],
        "equity_value_range": ["450000.00", "750000.00"],
        "sources_and_uses": {
            "purchase_price": "800000.00",
            "fees": "30000.00",
            "working_capital": "50000.00",
            "integration_cost": "0.00",
            "total_uses": "880000.00",
            "senior_debt": "400000.00",
            "seller_note": "0.00",
            "buyer_equity_required": "480000.00",
        },
        "annual_debt_service": "77227.58",
        "scenarios": {
            "base": {
                "ebitda": "150000.00",
                "cash_available_for_debt_service": "106500.00",
                "cash_after_debt_service": "29272.42",
                "coverage_ratio": "1.3790",
                "cash_shortfall": False,
            },
            "downside": {
                "ebitda": "50000.00",
                "cash_available_for_debt_service": "31500.00",
                "cash_after_debt_service": "-45727.58",
                "coverage_ratio": "0.4079",
                "cash_shortfall": True,
            },
            "upside": {
                "ebitda": "200000.00",
                "cash_available_for_debt_service": "144000.00",
                "cash_after_debt_service": "66772.42",
                "coverage_ratio": "1.8646",
                "cash_shortfall": False,
            },
        },
    }
    # The demo gives every figure main had, with the values it used on main, so it lists none of them as
    # assumed. It gives none of the inputs 1.1.0 added, so its request, and its idempotency fingerprint,
    # are main's; the ones the model used are listed.
    assert deal["data"]["assumed_inputs"] == [
        "real_estate_price",
        "property_debt",
        "investor_equity",
        "annual_rent",
    ]


# 8. Financing is the buyer's choice: a deal given no loan is screened as bought without debt.

NO_DEBT = (
    "No debt was given and none was assumed, so the deal is screened as bought without debt. "
    "There is no debt service: cash after debt service equals cash available, and there is no coverage ratio."
)
LOAN_TERMS = ["debt_rate", "debt_years", "seller_rate", "seller_years"]


def debt_line(content):
    """The paragraph under the memo's debt service heading."""
    lines = content.splitlines()
    start = lines.index("### Debt service and scenario coverage")
    return lines[start + 2]


@pytest.mark.parametrize("price", ["175000", "300000"])
def test_a_small_deal_with_no_financing_given_is_accepted_without_debt(client, setup, price):
    store, _, _, bid, _ = setup
    response = post(client, bid, {**MINIMAL, "purchase_price": price})
    assert response.status_code == 201, response.text
    deal = response.json()
    assert deal["data"]["assumed_inputs"] == MINIMAL_ASSUMED
    assert deal["data"]["inputs"]["senior_debt"] == "0" and deal["data"]["inputs"]["seller_note"] == "0"
    assert not set(LOAN_TERMS) & set(deal["data"]["assumed_inputs"])
    a = deal["data"]["analysis"]
    # Uses: the price, the example fees 30,000 and working capital 50,000; funded wholly by the buyer.
    total = {"175000": "255000.00", "300000": "380000.00"}[price]
    assert a["sources_and_uses"]["total_uses"] == total
    assert a["sources_and_uses"]["senior_debt"] == "0.00" and a["sources_and_uses"]["seller_note"] == "0.00"
    assert a["sources_and_uses"]["buyer_equity_required"] == total
    assert a["annual_debt_service"] == "0.00"
    # EBITDA 180,000 less 25% tax (no interest) and the example capex 15,000; downside less 10% of
    # revenue 900,000, upside plus 5% of it.
    assert a["scenarios"] == {
        "base": {
            "ebitda": "180000.00",
            "cash_available_for_debt_service": "120000.00",
            "cash_after_debt_service": "120000.00",
            "coverage_ratio": None,
            "cash_shortfall": False,
            "amortizing_coverage_ratio": None,
        },
        "downside": {
            "ebitda": "90000.00",
            "cash_available_for_debt_service": "52500.00",
            "cash_after_debt_service": "52500.00",
            "coverage_ratio": None,
            "cash_shortfall": False,
            "amortizing_coverage_ratio": None,
        },
        "upside": {
            "ebitda": "225000.00",
            "cash_available_for_debt_service": "153750.00",
            "cash_after_debt_service": "153750.00",
            "coverage_ratio": None,
            "cash_shortfall": False,
            "amortizing_coverage_ratio": None,
        },
    }
    content = memo(client, bid, deal["id"])
    assert debt_line(content) == NO_DEBT
    assert "Annual debt service:" not in content and "Debt service due in year 1" not in content
    base = next(line for line in content.splitlines() if line.startswith("| Base "))
    assert [cell.strip() for cell in base.strip("|").split("|")] == [
        "Base",
        "180,000.00",
        "120,000.00",
        "120,000.00",
        "No debt service",
        "No",
    ]
    assert inputs_section(content) == MINIMAL_SENTENCE


def test_a_deal_with_a_loan_keeps_its_annual_debt_service_line(client, setup):
    _, _, _, bid, _ = setup
    deal = post(client, bid, MAIN_SENT).json()
    # From acquisition 1.1.0 the line names the year it screens; the amount is main's.
    assert debt_line(memo(client, bid, deal["id"])) == (
        "Debt service due in year 1, the year screened: 102,304.37 USD."
    )


def test_a_given_loan_lists_its_rate_and_term_when_they_are_not_given(client, setup):
    _, _, _, bid, _ = setup
    senior = post(client, bid, {**MINIMAL, "senior_debt": "300000"}).json()
    assumed = senior["data"]["assumed_inputs"]
    assert [x for x in assumed if x in LOAN_TERMS] == ["debt_rate", "debt_years"]
    assert "senior_debt" not in assumed and "seller_note" in assumed
    assert senior["data"]["inputs"]["debt_rate"] == "0.09" and senior["data"]["inputs"]["debt_years"] == 7

    seller = post(client, bid, {**MINIMAL, "seller_note": "100000"}).json()
    assumed = seller["data"]["assumed_inputs"]
    assert [x for x in assumed if x in LOAN_TERMS] == ["seller_rate", "seller_years"]
    assert "seller_note" not in assumed and "senior_debt" in assumed

    # A loan given with its rate but not its term lists only the term.
    rate_only = post(client, bid, {**MINIMAL, "senior_debt": "300000", "debt_rate": "0.08"}).json()
    assert [x for x in rate_only["data"]["assumed_inputs"] if x in LOAN_TERMS] == ["debt_years"]
    content = memo(client, bid, senior["id"])
    assert debt_line(content).startswith("Debt service due in year 1, the year screened: ")
    assert "Senior annual interest rate 9.00%; Senior term 7 years" in inputs_section(content)


def test_a_rate_and_term_without_their_loan_change_no_figure():
    plain = acquisition(DealInputs(**MINIMAL), set())
    odd = acquisition(
        DealInputs(
            **MINIMAL,
            debt_rate="0.5",
            debt_years=1,
            property_rate="0.3",
            property_years=3,
            seller_rate="0.4",
            seller_years=2,
            seller_schedule="deferred",
        ),
        set(),
    )
    assert figures(odd) == figures(plain)
    assert odd["annual_debt_service"] == "0.00"
    assert all(s["coverage_ratio"] is None for s in odd["scenarios"].values())


# 9. A refusal caused by a figure the caller did not give names it and its example value.

OLD_MULTIPLES = "High multiple must not be below low multiple."
HIGH_ASSUMED = (
    "High multiple must not be below low multiple. The high multiple was not given, so its example "
    "value 5 was used; give a high multiple."
)
LOW_ASSUMED = (
    "High multiple must not be below low multiple. The low multiple was not given, so its example "
    "value 3 was used; give a low multiple."
)
OLD_DEBT = "Debt funding exceeds transaction uses."
FEES_AND_WC_ASSUMED = (
    "Debt funding exceeds transaction uses. The transaction fees and the working capital funding were "
    "not given, so their example values 30,000 and 50,000 were used; give both."
)
WC_ASSUMED = (
    "Debt funding exceeds transaction uses. The working capital funding was not given, so its example "
    "value 50,000 was used; give the working capital funding."
)
FEES_ASSUMED = (
    "Debt funding exceeds transaction uses. The transaction fees were not given, so their example "
    "value 30,000 was used; give the transaction fees."
)
OLD_EBITDA = "Reported EBITDA exceeds revenue; review the inputs."
# Each refused request, with the one message it must give. MINIMAL's price is 700,000.
REFUSALS = [
    ({"multiple_low": "6"}, HIGH_ASSUMED),
    ({"multiple_high": "2"}, LOW_ASSUMED),
    ({"multiple_low": "6", "multiple_high": "5"}, OLD_MULTIPLES),
    ({"senior_debt": "790000"}, FEES_AND_WC_ASSUMED),
    ({"senior_debt": "750001", "fees": "0"}, WC_ASSUMED),
    ({"seller_note": "730001", "working_capital": "0"}, FEES_ASSUMED),
    ({"senior_debt": "700001", "fees": "0", "working_capital": "0"}, OLD_DEBT),
    ({"reported_ebitda": "900001"}, OLD_EBITDA),
]
REFUSAL_IDS = ["high", "low", "both multiples", "fees and wc", "wc", "fees", "all given", "ebitda"]


@pytest.mark.parametrize(("change", "message"), REFUSALS, ids=REFUSAL_IDS)
def test_the_api_refusal_names_each_assumed_figure_it_counted(client, setup, change, message):
    store, _, _, bid, _ = setup
    before = counts(store)
    response = post(client, bid, {**MINIMAL, **change}, key="refused-key")
    assert response.status_code == 422
    assert response.json()["detail"] == "body: Value error, " + message
    assert counts(store) == before
    # Nothing was stored and the key was not used: the same key then saves a valid request.
    assert post(client, bid, MINIMAL, key="refused-key").status_code == 201


def test_a_low_multiple_with_a_high_multiple_above_it_is_accepted(client, setup):
    _, _, _, bid, _ = setup
    response = post(client, bid, {**MINIMAL, "multiple_low": "6", "multiple_high": "7"})
    assert response.status_code == 201, response.text
    deal = response.json()
    assert "multiple_low" not in deal["data"]["assumed_inputs"]
    assert "multiple_high" not in deal["data"]["assumed_inputs"]
    assert deal["data"]["analysis"]["enterprise_value_range"] == ["1080000.00", "1260000.00"]


def test_the_cli_gives_each_refusal_as_one_line(tmp_path, capsys):
    db = str(tmp_path / "cli" / "foundry.db")
    cli.main(["--db", db, "init"])
    business = tmp_path / "business.json"
    business.write_text(
        json.dumps({"name": "CLI refusal", "concept": "A synthetic business for CLI refusals."})
    )
    capsys.readouterr()
    cli.main(["--db", db, "create", str(business)])
    bid = json.loads(capsys.readouterr().out)["id"]
    store = Store(db)
    before = counts(store)
    deal = tmp_path / "deal.json"
    for change, message in REFUSALS:
        deal.write_text(json.dumps({**MINIMAL, **change}))
        with pytest.raises(SystemExit) as stopped:
            cli.main(["--db", db, "acquire", bid, str(deal)])
        assert stopped.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"Foundry: deal.json was not accepted: Value error, {message}\n"
    assert counts(store) == before


def test_the_mcp_tool_passes_back_each_refusal(setup):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(owner, {"name": "Refusal agent", "role": "agent", "business_ids": [bid]})
    texts = []

    async def check(url):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={**os.environ, "FOUNDRY_API_URL": url, "FOUNDRY_API_TOKEN": agent["token"]},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                for change, _ in REFUSALS:
                    result = await client.call_tool(
                        "analyze_acquisition",
                        {"business_id": bid, "command_key": "mcp-refused", "inputs": {**MINIMAL, **change}},
                    )
                    assert result.isError
                    texts.append(result.content[0].text)

    before = counts(store)
    with api_server(store) as url:
        asyncio.run(check(url))
    for text, (_, message) in zip(texts, REFUSALS, strict=True):
        assert "body: Value error, " + message in text
        # The assumed sentence appears only where an example value was counted.
        if message in {OLD_MULTIPLES, OLD_DEBT, OLD_EBITDA}:
            assert "not given" not in text
    assert counts(store) == before


# 10. `foundry demo` can be run again: on a database an earlier version seeded, and on its own.

# The deal request main (e2317d3) sent for the demo: its DealInputs dumped with every default filled in.
# Written out by hand, so a change to this branch's demo cannot change what the test seeds.
MAIN_DEMO_DEAL = {
    "target": "Example Mapping Studio · synthetic",
    "thesis": "Compare buying a small mapping-services operation with building delivery capacity. All figures are illustrative.",
    "annual_revenue": "1000000",
    "reported_ebitda": "200000",
    "adjustments": [
        {"label": "Illustrative one-time expense", "amount": "10000", "evidence_id": "EVIDENCE"},
        {"label": "Unsupported seller add-back", "amount": "25000", "evidence_id": None},
    ],
    "replacement_management": "60000",
    "multiple_low": "3",
    "multiple_high": "5",
    "purchase_price": "800000",
    "fees": "30000",
    "working_capital": "50000",
    "integration_cost": "0",
    "senior_debt": "400000",
    "debt_rate": "0.09",
    "debt_years": 7,
    "seller_note": "0",
    "seller_rate": "0.06",
    "seller_years": 5,
    "annual_capex": "15000",
    "annual_working_capital_growth": "0",
    "tax_rate": "0.25",
    "cash_acquired": "0",
    "debt_assumed": "0",
    "evidence_ids": [],
}


def run_cli(args, capsys):
    """The exit status of `foundry` with these arguments, and what it printed."""
    try:
        cli.main(args)
        code = 0
    except SystemExit as stopped:
        code = stopped.code
    return code, capsys.readouterr()


def verify_demo(db, capsys):
    store = Store(db)
    with store.read() as con:
        (bid,) = con.execute("SELECT id FROM businesses").fetchone()
    code, out = run_cli(["--db", db, "verify", bid], capsys)
    assert code == 0, out.err
    assert json.loads(out.out)["valid"] is True


def test_the_demo_runs_again_on_a_database_seeded_by_main(tmp_path, capsys):
    db = str(tmp_path / "upgraded" / "foundry.db")
    assert run_cli(["--db", db, "init"], capsys)[0] == 0
    store = Store(db)
    service = Service(store)
    actor = store.authenticate((tmp_path / "upgraded" / "owner.key").read_text().strip())

    # Seed as main did: its demo, with the deal saved from main's request by save_deal as it was then.
    def save_deal_as_on_main(actor, bid, key, payload):
        request = json.loads(json.dumps(MAIN_DEMO_DEAL))
        request["adjustments"][0]["evidence_id"] = payload["adjustments"][0]["evidence_id"]
        return save_deal_as_before(service, actor, bid, key, request)

    service.save_deal = save_deal_as_on_main
    seed(service, actor)
    before = counts(store)
    with store.read() as con:
        stored = con.execute("SELECT id, version, data FROM objects WHERE kind='deal'").fetchall()
    assert len(stored) == 1 and "assumed_inputs" not in json.loads(stored[0]["data"])

    code, out = run_cli(["--db", db, "demo"], capsys)
    assert code == 0, out.err
    assert "Foundry:" not in out.err
    assert counts(store) == before
    with store.read() as con:
        assert [tuple(r) for r in con.execute("SELECT id, version, data FROM objects WHERE kind='deal'")] == [
            tuple(r) for r in stored
        ]
    verify_demo(db, capsys)


def test_the_demo_runs_twice_on_a_fresh_database(tmp_path, capsys):
    db = str(tmp_path / "fresh" / "foundry.db")
    code, out = run_cli(["--db", db, "demo"], capsys)
    assert code == 0, out.err
    store = Store(db)
    first = counts(store)
    assert first["objects"] > 0 and kinds(store, "deal") == 1

    code, out = run_cli(["--db", db, "demo"], capsys)
    assert code == 0, out.err
    assert "Foundry:" not in out.err
    assert counts(store) == first
    verify_demo(db, capsys)


# The example values of DealInputs on main before this change, written out by hand from
# src/foundry/models.py at e2317d3 (the same values in every earlier commit). The web form compares an
# analysis saved without assumed_inputs with these, so a figure left out then starts empty when reopened.
MAIN_EXAMPLES = {
    "annual_revenue": "1000000",
    "reported_ebitda": "200000",
    "replacement_management": "0",
    "multiple_low": "3",
    "multiple_high": "5",
    "purchase_price": "800000",
    "fees": "30000",
    "working_capital": "50000",
    "integration_cost": "0",
    "senior_debt": "400000",
    "debt_rate": "0.09",
    "debt_years": "7",
    "seller_note": "0",
    "seller_rate": "0.06",
    "seller_years": "5",
    "annual_capex": "15000",
    "annual_working_capital_growth": "0",
    "tax_rate": "0.25",
    "cash_acquired": "0",
    "debt_assumed": "0",
}
APP_JS = Path(__file__).resolve().parents[1] / "src" / "foundry" / "static" / "app.js"


def js_constant(name: str) -> str:
    source = APP_JS.read_text()
    start = source.index(f"const {name} = ")
    return source[start : source.index("\n};\n", start)]


def test_the_web_forms_earlier_example_values_are_mains():
    pairs = re.findall(r"^\s+(\w+): '([^']*)',$", js_constant("DEAL_EXAMPLES_BEFORE_LIST"), re.M)
    assert dict(pairs) == MAIN_EXAMPLES and len(pairs) == len(MAIN_EXAMPLES)
    # Today's model differs from main only where this change made it differ: the three required figures
    # have no example, and the example senior debt is zero. The inputs acquisition 1.1.0 added are new.
    fields = DealInputs.model_fields
    assert list(MAIN_EXAMPLES) == [
        n
        for n in fields
        if n not in {"target", "thesis", "adjustments", "evidence_ids", *models.DEAL_FIELDS_ADDED_IN_1_1}
    ]
    changed = {}
    for name, value in MAIN_EXAMPLES.items():
        today = None if fields[name].is_required() else str(fields[name].default)
        if today is None or Decimal(today) != Decimal(value):
            changed[name] = today
    assert changed == {
        "annual_revenue": None,
        "reported_ebitda": None,
        "purchase_price": None,
        "senior_debt": "0",
    }


def test_the_web_forms_earlier_example_values_match_main_at_e2317d3():
    shown = subprocess.run(
        ["git", "show", "e2317d3:src/foundry/models.py"],
        cwd=APP_JS.parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode:
        pytest.skip("commit e2317d3 is not in this checkout")
    tree = ast.parse(shown.stdout)
    (deal,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DealInputs"]
    defaults = {}
    for item in deal.body:
        if isinstance(item, ast.AnnAssign) and item.value is not None:
            value = item.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "Decimal":
                defaults[item.target.id] = value.args[0].value
            elif isinstance(value, ast.Constant):
                defaults[item.target.id] = str(value.value)
    assert defaults == MAIN_EXAMPLES


def test_the_web_forms_examples_are_todays_model():
    # dealFields' third column is today's example value; for the three required figures it is only the
    # placeholder, main's example. The seller note schedule's example is a word, not a number.
    rows = re.findall(r"^\s+\['(\w+)', '[^']*', ([\d.]+|'\w+'), \w+\],$", js_constant("dealFields"), re.M)
    assert [name for name, _ in rows] == [
        n for n in DealInputs.model_fields if n not in {"target", "thesis", "adjustments", "evidence_ids"}
    ]
    for name, example in rows:
        field = DealInputs.model_fields[name]
        expected = MAIN_EXAMPLES[name] if field.is_required() else field.default
        if example.startswith("'"):
            assert example.strip("'") == expected, name
        else:
            assert Decimal(example) == Decimal(str(expected)), name
