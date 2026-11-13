"""An acquisition analysis never runs on figures nobody gave.

The target's revenue and reported EBITDA and the purchase price are required. Every other figure the
caller leaves out takes the model's example value, and the deal records it in assumed_inputs, the same
way through the API, the command line and the MCP adapter. Expected values are written out by hand.
"""

import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
import time
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
from foundry.store import Store

# Every figure, each different from its example value.
FULL = {
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
# The same deal with no evidence to look up, for requests that go through the service.
SENT = {**FULL, "adjustments": [{"label": "Unreviewed", "amount": "5000", "evidence_id": None}]}
MINIMAL = {
    "target": "Synthetic minimal target",
    "thesis": "A synthetic deal that gives only the required figures.",
    "annual_revenue": "900000",
    "reported_ebitda": "180000",
    "purchase_price": "700000",
}
# Every figure with an example value, in the field order of DealInputs.
MINIMAL_ASSUMED = [
    "replacement_management",
    "multiple_low",
    "multiple_high",
    "fees",
    "working_capital",
    "integration_cost",
    "senior_debt",
    "debt_rate",
    "debt_years",
    "seller_note",
    "seller_rate",
    "seller_years",
    "annual_capex",
    "annual_working_capital_growth",
    "tax_rate",
    "cash_acquired",
    "debt_assumed",
]
MINIMAL_SENTENCE = (
    "Not given, so the model's example value was used: Annual replacement management cost 0.00; "
    "Low EBITDA multiple 3.00x; High EBITDA multiple 5.00x; Transaction fees 30,000.00; "
    "Working capital funding 50,000.00; Integration costs 0.00; New senior debt 400,000.00; "
    "Senior annual interest rate 9.00%; Senior term 7 years; Seller financing 0.00; "
    "Seller annual interest rate 6.00%; Seller term 5 years; Annual maintenance capex 15,000.00; "
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
        "working_capital",
        "integration_cost",
        "senior_debt",
        "debt_rate",
        "seller_rate",
        "seller_years",
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
        "Every input was given. The model used none of its example values."
    )


# 3. Deals stored before this change still load.


def save_deal_as_before(service, actor, bid, key, payload):
    """save_deal as it was before assumed_inputs existed (e2317d3): the payload was the full dump."""

    def run(con):
        ids = payload["evidence_ids"] + [
            a["evidence_id"] for a in payload["adjustments"] if a.get("evidence_id")
        ]
        evidence = service.references(con, bid, ids, "evidence")
        reviewed = {e["id"] for e in evidence if e["state"] == "verified"}
        result = finance.acquisition(DealInputs.model_validate(payload), reviewed)
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
    old = save_deal_as_before(service, actor, bid, str(uuid4()), DealInputs(**SENT).model_dump(mode="json"))
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
    old = save_deal_as_before(service, actor, bid, str(uuid4()), DealInputs(**SENT).model_dump(mode="json"))
    snapshot = service.snapshot(actor, bid)
    deal = next(o for o in snapshot["objects"] if o["id"] == old["id"])
    _, content = render_document("investment_memo", snapshot, deal)
    assert inputs_section(content) == NOT_RECORDED


# 7. The arithmetic is unchanged: figures computed on main (e2317d3) for a fixed full payload and the demo.


def figures(analysis):
    return {k: v for k, v in analysis.items() if k not in {"inputs", "limitations"}}


def test_figures_for_a_fixed_full_payload_are_unchanged_from_main():
    result = acquisition(DealInputs(**FULL), {"ev1"})
    assert figures(result) == {
        "model_version": "1.0.1",
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


def test_the_demo_deal_has_the_same_figures_as_on_main(setup):
    _, service, actor, _, _ = setup
    business = seed(service, actor)
    deal = next(o for o in service.snapshot(actor, business["id"])["objects"] if o["kind"] == "deal")
    analysis = figures(deal["data"]["analysis"])
    for item in analysis["accepted_adjustments"]:
        item["evidence_id"] = "reviewed"
    assert analysis == {
        "model_version": "1.0.1",
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
    # The demo gives revenue, EBITDA, price and replacement management; everything else is assumed.
    assert deal["data"]["assumed_inputs"] == MINIMAL_ASSUMED[1:]
