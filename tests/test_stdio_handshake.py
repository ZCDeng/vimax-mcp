"""End-to-end stdio handshake: spawn the server as a subprocess and talk MCP to it.

This verifies the JSON-RPC framing on stdio works, tools are listed, and a
trivial tool call (get_job_status on a missing job) returns gracefully.
We never invoke submit_idea2video here — that would trigger ViMax import
and consume API quota.
"""

import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_list_tools_via_stdio():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vimax_mcp.server", "--transport", "stdio"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {t.name for t in result.tools}
            assert names == {
                "submit_idea2video",
                "submit_script2video",
                "get_job_status",
                "list_artifacts",
                "cancel_job",
                "get_quota",
            }


@pytest.mark.asyncio
async def test_get_status_missing_job_via_stdio():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vimax_mcp.server", "--transport", "stdio"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(
                "get_job_status", {"job_id": "DOES_NOT_EXIST"}
            )
            assert not res.isError
            # FastMCP returns a JSON-encoded dict in the text content
            import json

            payload = json.loads(res.content[0].text)
            assert "error" in payload
            assert "not found" in payload["error"]


@pytest.mark.asyncio
async def test_get_quota_via_stdio():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vimax_mcp.server", "--transport", "stdio"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("get_quota", {})
            assert not res.isError
            import json

            payload = json.loads(res.content[0].text)
            assert "date" in payload
            for provider in ("chat", "image", "video"):
                assert provider in payload
                assert "used_today" in payload[provider]
                assert "limit" in payload[provider]
