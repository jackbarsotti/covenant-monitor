"""EDGAR access for the graph, through our MCP server.

In production the server runs as a subprocess over stdio. Tests hand the MCP client the
server object itself, so the same tool calls run in-process.
"""

import os
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
    # The subprocess only inherits a minimal environment. Pass the SEC User-Agent through
    # when it is set (CI sets it as a secret; locally the server reads .env itself).
    env = {k: os.environ[k] for k in ("SEC_USER_AGENT",) if k in os.environ}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "covenant_monitor.mcp_server.server"], env=env
    )
    async with Client(params) as client:
        yield EdgarTools(client)
