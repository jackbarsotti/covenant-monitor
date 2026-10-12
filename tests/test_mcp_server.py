import httpx
import pytest
import respx
from helpers import fixture_json, fixture_text
from mcp.server.mcpserver.exceptions import ToolError

from covenant_monitor.mcp_server.server import build_server

pytestmark = pytest.mark.anyio

DOC_URL = "https://www.sec.gov/Archives/edgar/data/885725/000094787120000527/ss174648_ex1002.htm"


@pytest.fixture
def server(client):
    return build_server(client)


@pytest.fixture
def sec():
    """Every SEC endpoint the tools use, answered from saved live responses."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
            return_value=httpx.Response(200, json=fixture_json("company_tickers_exchange.json"))
        )
        mock.get("https://data.sec.gov/submissions/CIK0000885725.json").mock(
            return_value=httpx.Response(200, json=fixture_json("submissions_bsx.json"))
        )
        mock.get("https://efts.sec.gov/LATEST/search-index").mock(
            return_value=httpx.Response(200, json=fixture_json("efts_bsx_credit_agreement.json"))
        )
        mock.get(DOC_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("exhibit_bsx.htm"))
        )
        mock.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000885725.json").mock(
            return_value=httpx.Response(200, json=fixture_json("companyfacts_bsx.json"))
        )
        yield mock


async def call(server, name, **args):
    result = await server.call_tool(name, args)
    assert not result.is_error
    return result.structured_content


async def test_lists_all_five_read_only_tools(server):
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == {
        "lookup_company",
        "list_filings",
        "search_filings_text",
        "get_document_text",
        "get_annual_financials",
    }
    assert all(t.annotations.read_only_hint for t in tools.values())


async def test_lookup_company(server, sec):
    out = await call(server, "lookup_company", ticker="bsx")
    assert out == {
        "cik": 885725,
        "name": "BOSTON SCIENTIFIC CORP",
        "ticker": "BSX",
        "exchange": "NYSE",
    }


async def test_lookup_company_accepts_dotted_class_tickers(server, sec):
    out = await call(server, "lookup_company", ticker="BRK.B")
    assert out["ticker"] == "BRK-B"


async def test_list_filings(server, sec):
    out = await call(server, "list_filings", cik="0000885725", form_types=["10-K", "8-K"])
    filing = out["filings"][0]
    assert set(filing) == {"accession", "form", "filed", "primary_document_url"}
    assert filing["primary_document_url"].startswith(
        "https://www.sec.gov/Archives/edgar/data/885725/"
    )


async def test_search_filings_text(server, sec):
    out = await call(server, "search_filings_text", cik=885725, query='"credit agreement"')
    assert out["total"] > 0
    exhibit = out["hits"][0]
    assert exhibit["exhibit_type"].startswith("EX-10")
    assert "CREDIT AGREEMENT" in exhibit["description"]
    # Filed by an agent: the accession prefix differs, but the path uses the company's CIK.
    assert exhibit["url"] == DOC_URL
    assert sec.calls.last.request.url.params["ciks"] == "0000885725"


async def test_get_document_text_strips_html_and_pages(server, sec):
    out = await call(server, "get_document_text", url=DOC_URL)
    assert "SECOND AMENDMENT" in out["text"]
    assert "<P" not in out["text"] and "&ldquo;" not in out["text"]
    assert out["offset"] == 0 and out["total_chars"] == len(out["text"])

    tail = await call(server, "get_document_text", url=DOC_URL, offset=out["total_chars"] - 10)
    assert len(tail["text"]) == 10 and tail["next_offset"] is None


async def test_get_annual_financials(server, sec):
    out = await call(
        server,
        "get_annual_financials",
        cik=885725,
        concepts=["OperatingIncomeLoss", "NotARealConcept"],
    )
    op = out["values"]["OperatingIncomeLoss"]
    assert set(op) == {"value", "period_start", "period_end", "form", "filed", "accession"}
    assert op["form"] in ("10-K", "10-K/A")
    assert out["values"]["NotARealConcept"] is None


@pytest.mark.parametrize("ticker", ["", "TOOLONGX", "BSX;rm", "../x", "B S X"])
async def test_rejects_malformed_tickers(server, ticker):
    with pytest.raises(ToolError):
        await server.call_tool("lookup_company", {"ticker": ticker})


async def test_rejects_non_sec_urls_without_fetching(server, sec):
    with pytest.raises(ToolError, match="www.sec.gov/Archives"):
        await server.call_tool("get_document_text", {"url": "https://example.com/x.htm"})
    assert not sec.calls


@pytest.mark.parametrize(
    "name,args",
    [
        ("list_filings", {"cik": "abc", "form_types": ["10-K"]}),
        ("list_filings", {"cik": 885725, "form_types": ["10-K"], "since": "last year"}),
        ("get_annual_financials", {"cik": 885725, "concepts": ["Operating Income"]}),
        ("get_annual_financials", {"cik": 885725, "concepts": []}),
        ("get_document_text", {"url": DOC_URL, "offset": -1}),
    ],
)
async def test_rejects_bad_inputs(server, name, args):
    with pytest.raises(ToolError):
        await server.call_tool(name, args)


async def test_unknown_ticker_is_a_short_message(server, sec):
    with pytest.raises(ToolError) as err:
        await server.call_tool("lookup_company", {"ticker": "ZZZZ"})
    assert "ZZZZ not found" in str(err.value)
    assert "Traceback" not in str(err.value)
