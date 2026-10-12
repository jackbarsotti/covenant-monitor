"""EDGAR access for the graph, through our MCP server.

In production the server runs as a subprocess over stdio. Tests hand the MCP client the
server object itself, so the same tool calls run in-process.
"""

import sys
from contextlib import asynccontextmanager

from mcp import Client, StdioServerParameters

from covenant_monitor.agent.llm import AgentStop


class EdgarTools:
    def __init__(self, client: Client):
        self.client = client

    async def call(self, name: str, **arguments) -> dict:
        result = await self.client.call_tool(name, arguments)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content).strip()
            raise AgentStop(f"{name} failed: {text or 'no details'}")
        return result.structured_content


@asynccontextmanager
async def stdio_tools():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "covenant_monitor.mcp_server.server"]
    )
    async with Client(params) as client:
        yield EdgarTools(client)
