import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
import time

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from foundry.api import create_app


def test_actual_mcp_stdio_roundtrip_and_agent_authority(setup):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(
        owner, {"name": "Protocol test agent", "role": "agent", "business_ids": [bid]}
    )
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

    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={
                **os.environ,
                "FOUNDRY_API_URL": f"http://127.0.0.1:{port}",
                "FOUNDRY_API_TOKEN": agent["token"],
            },
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                names = {t.name for t in (await client.list_tools()).tools}
                assert names == {
                    "list_businesses",
                    "get_business",
                    "add_record",
                    "update_record",
                    "add_evidence",
                    "prepare_task",
                    "build_forecast",
                    "analyze_acquisition",
                    "generate_document",
                }
                result = await client.call_tool("get_business", {"business_id": bid})
                assert not result.isError
                body = json.loads(result.content[0].text)
                assert body["business"]["id"] == bid
                result = await client.call_tool(
                    "add_record",
                    {
                        "business_id": bid,
                        "command_key": "mcp-test",
                        "record": {
                            "kind": "offer",
                            "title": "Synthetic MCP offer",
                            "details": "An explicitly unverified offer.",
                        },
                    },
                )
                assert not result.isError
                result = await client.call_tool(
                    "add_record",
                    {
                        "business_id": bid,
                        "command_key": "mcp-deny",
                        "record": {
                            "kind": "offer",
                            "title": "Attempted self-approval",
                            "certainty": "verified",
                        },
                    },
                )
                assert result.isError

    try:
        asyncio.run(check())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive()


@contextlib.contextmanager
def foundry_api(store):
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
    assert not thread.is_alive()


def run_client(url, token, check):
    async def main():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={**os.environ, "FOUNDRY_API_URL": url, "FOUNDRY_API_TOKEN": token},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await check(client)

    asyncio.run(main())


def test_mcp_update_record_replaces_in_place_and_keeps_agent_limits(setup):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(owner, {"name": "Update agent", "role": "agent", "business_ids": [bid]})

    def fields(record_id, version, **changes):
        return {
            "business_id": bid,
            "record_id": record_id,
            "command_key": f"update-{version}-{changes.get('certainty', 'x')}",
            "expected_version": version,
            "kind": "customer",
            "title": "Refined buyer",
            "details": "Regional planning teams with permit backlogs.",
            "certainty": "assumption",
            "owner": "Founder",
            "evidence_ids": [],
            "related_ids": [],
            "due_date": None,
            "properties": {},
        } | changes

    async def check(client):
        result = await client.call_tool(
            "add_record",
            {
                "business_id": bid,
                "command_key": "draft-customer",
                "record": {"kind": "customer", "title": "Draft buyer", "details": "Unclear."},
            },
        )
        assert not result.isError
        draft = json.loads(result.content[0].text)
        assert draft["version"] == 1
        result = await client.call_tool("update_record", fields(draft["id"], 1))
        assert not result.isError
        updated = json.loads(result.content[0].text)
        assert updated["id"] == draft["id"]
        assert updated["version"] == 2
        assert updated["title"] == "Refined buyer"
        assert updated["data"]["details"] == "Regional planning teams with permit backlogs."
        body = json.loads((await client.call_tool("get_business", {"business_id": bid})).content[0].text)
        assert len([o for o in body["objects"] if o["kind"] == "customer"]) == 1

        stale = await client.call_tool("update_record", fields(draft["id"], 1, command_key="stale"))
        assert stale.isError
        verified = await client.call_tool("update_record", fields(draft["id"], 2, certainty="verified"))
        assert verified.isError
        missing = fields(draft["id"], 2, command_key="missing")
        del missing["properties"]
        assert (await client.call_tool("update_record", missing)).isError
        for bad in ["../etc", "customer_1?x=1", "customer%2F1", "a/b", "x#y", "a.b", ""]:
            result = await client.call_tool("update_record", fields(bad, 2, command_key="bad-" + bad))
            assert result.isError
            assert "Invalid record ID" in result.content[0].text
        current = next(o for o in service.snapshot(owner, bid)["objects"] if o["id"] == draft["id"])
        assert current["version"] == 2 and current["state"] == "assumption"

    with foundry_api(store) as url:
        run_client(url, agent["token"], check)


def test_mcp_guided_prompts_are_static_and_state_human_review(setup):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(owner, {"name": "Prompt agent", "role": "agent", "business_ids": [bid]})

    async def check(client):
        names = {p.name for p in (await client.list_prompts()).prompts}
        assert names == {"founder_intake", "acquisition_intake", "readiness_review"}
        for name, topics in [
            (
                "founder_intake",
                ["buyer", "problem", "pricing", "third-party rights", "build_forecast", "add_evidence"],
            ),
            (
                "acquisition_intake",
                [
                    "thesis",
                    "annual revenue, reported EBITDA and the negotiated purchase price",
                    "never invent, estimate or fill in any of the three",
                    "replacement management",
                    "analyze_acquisition",
                ],
            ),
            ("readiness_review", ["blockers", "stale", "ask before"]),
        ]:
            prompt = await client.get_prompt(name, {"business_id": bid})
            text = prompt.messages[0].content.text
            assert bid in text
            assert "get_business" in text and "untrusted" in text
            assert "update_record" in text and "expected_version" in text
            assert "Never use certainty='verified'" in text
            assert "human owner or reviewer must approve" in text
            assert "prepare_task" in text
            assert "Test business" not in text  # no stored business content
            for topic in topics:
                assert topic in text
        for bad in ["../business_x", "customer_1", "business_1/x"]:
            with pytest.raises(Exception, match="Invalid business ID"):
                await client.get_prompt("founder_intake", {"business_id": bad})

    with foundry_api(store) as url:
        run_client(url, agent["token"], check)
