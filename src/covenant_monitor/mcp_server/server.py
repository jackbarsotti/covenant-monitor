"""Read-only EDGAR tools for the agent, served over stdio.

All tools share one SECClient, so the rate limit and cache cover every call. Inputs are
validated by pydantic from the type hints. Failures come back as short ToolError messages
the model can act on; anything unexpected reaches the model only as a generic error.

    npx @modelcontextprotocol/inspector uv run covenant-monitor-mcp
"""

import sys
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from covenant_monitor import edgar
from covenant_monitor.sec_client import SECClient, SECError

Ticker = Annotated[
    str,
    Field(pattern=r"^[A-Za-z]{1,5}([.-][A-Za-z]{1,2})?$", description="Stock ticker, e.g. BSX"),
]
Cik = Annotated[int, Field(ge=1, le=9_999_999_999, description="SEC Central Index Key")]
FormType = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 /-]{0,14}$", description="e.g. 10-K, 8-K")
]
Forms = Annotated[list[FormType], Field(max_length=10)]
Concept = Annotated[
    str, Field(pattern=r"^[A-Z][A-Za-z0-9]{1,150}$", description="us-gaap concept name")
]
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


class Company(BaseModel):
    cik: int
    name: str
    ticker: str
    exchange: str | None


class Filing(BaseModel):
    accession: str
    form: str
    filed: str
    primary_document_url: str


class FilingList(BaseModel):
    filings: list[Filing]


class SearchHit(BaseModel):
    accession: str
    form: str
    filed: str
    exhibit_type: str
    description: str | None
    url: str


class SearchResults(BaseModel):
    total: int
    hits: list[SearchHit]


class DocumentPage(BaseModel):
    url: str
    offset: int
    total_chars: int
    next_offset: int | None
    text: str


class AnnualValue(BaseModel):
    value: float
    period_start: str | None  # null for balance sheet (instant) values
    period_end: str
    form: str
    filed: str
    accession: str


class AnnualFinancials(BaseModel):
    cik: int
    name: str | None
    values: dict[str, AnnualValue | None]


def build_server(client: SECClient) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield None
        finally:
            await client.aclose()

    server = MCPServer(
        "covenant-monitor-edgar",
        instructions="Read-only access to SEC EDGAR filings and XBRL financials.",
        lifespan=lifespan,
    )

    @server.tool(annotations=READ_ONLY)
    async def lookup_company(ticker: Ticker) -> Company:
        """Find a company's CIK, name, and exchange from its ticker."""
        try:
            return Company(**await edgar.lookup_company(client, ticker.replace(".", "-")))
        except SECError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ_ONLY)
    async def list_filings(cik: Cik, form_types: Forms, since: date | None = None) -> FilingList:
        """List a company's filings of the given form types, newest first (max 100)."""
        try:
            return FilingList(filings=await edgar.list_filings(client, cik, form_types, since))
        except SECError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ_ONLY)
    async def search_filings_text(
        cik: Cik,
        query: Annotated[str, Field(min_length=1, max_length=200)],
        form_types: Forms = [],  # noqa: B006 - pydantic copies defaults
    ) -> SearchResults:
        """Full-text search of a company's filings. Each hit is one document, with its
        exhibit type (e.g. EX-10.1) and description. Wrap phrases in double quotes."""
        try:
            return SearchResults(**await edgar.search_filings_text(client, cik, query, form_types))
        except SECError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ_ONLY)
    async def get_document_text(
        url: Annotated[str, Field(max_length=500)],
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> DocumentPage:
        """Plain text of a filing document on sec.gov Archives, 20,000 characters per page.
        Pass next_offset back as offset to read the next page."""
        try:
            return DocumentPage(**await edgar.get_document_text(client, url, offset))
        except SECError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ_ONLY)
    async def get_annual_financials(
        cik: Cik, concepts: Annotated[list[Concept], Field(min_length=1, max_length=25)]
    ) -> AnnualFinancials:
        """Latest full-year 10-K value of each us-gaap concept, with its period and source
        accession. A concept the company doesn't report comes back as null."""
        try:
            return AnnualFinancials(**await edgar.get_annual_financials(client, cik, concepts))
        except SECError as e:
            raise ToolError(str(e)) from e

    return server


def main() -> None:
    try:
        client = SECClient()
    except RuntimeError as e:
        sys.exit(f"covenant-monitor-mcp: {e}")
    build_server(client).run("stdio")


if __name__ == "__main__":
    main()
