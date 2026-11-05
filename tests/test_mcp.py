import asyncio
import json
import os
import socket
import sys
import threading
import time

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
