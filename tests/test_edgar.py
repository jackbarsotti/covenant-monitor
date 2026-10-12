from datetime import date

import httpx
import pytest
import respx
from helpers import fixture_json

from covenant_monitor.edgar import (
    check_archive_url,
    find_company,
    html_to_text,
    latest_annual_value,
    list_filings,
    pad_cik,
)
from covenant_monitor.sec_client import SECError


def _facts(rows):
    return {"facts": {"us-gaap": {"OperatingIncomeLoss": {"units": {"USD": rows}}}}}


def _row(start, end, val, filed, form="10-K"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def test_pad_cik():
    assert pad_cik(320193) == "0000320193"
    assert pad_cik("0000320193") == "0000320193"


def test_find_company_is_case_insensitive():
    tickers = fixture_json("company_tickers_exchange.json")
    assert find_company(tickers, "bsx")["cik"] == 885725
    assert find_company(tickers, "BRK-B")["exchange"] == "NYSE"
    with pytest.raises(SECError):
        find_company(tickers, "NOPE")


def test_picks_latest_full_year_and_skips_quarters_and_10q():
    facts = _facts(
        [
            _row("2024-01-01", "2024-12-31", 100, "2025-02-20"),
            _row("2025-01-01", "2025-12-31", 150, "2026-02-26"),
            # Q4 fact that lives inside the 10-K: not a full year
            _row("2025-10-01", "2025-12-31", 40, "2026-02-26"),
            # Nine-month fact from a 10-Q: wrong form and wrong length
            _row("2026-01-01", "2026-09-30", 120, "2026-10-30", form="10-Q"),
        ]
    )
    picked = latest_annual_value(facts, "OperatingIncomeLoss")
    assert (picked["end"], picked["val"]) == ("2025-12-31", 150)


def test_newest_filing_wins_for_the_same_period():
    facts = _facts(
        [
            _row("2024-01-01", "2024-12-31", 100, "2025-02-20"),
            # Same FY2024 period, restated in a 10-K/A filed later
            _row("2024-01-01", "2024-12-31", 97, "2025-06-10", form="10-K/A"),
        ]
    )
    assert latest_annual_value(facts, "OperatingIncomeLoss")["val"] == 97


def test_missing_concept_returns_none():
    assert latest_annual_value({"facts": {"us-gaap": {}}}, "OperatingIncomeLoss") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/Archives/edgar/data/1/2/x.htm",
        "http://www.sec.gov/Archives/edgar/data/1/2/x.htm",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
        "https://www.sec.gov/Archives/edgar/data/../../files/x.json",
        "https://www.sec.gov.evil.com/Archives/edgar/data/1/2/x.htm",
        "https://user@www.sec.gov/Archives/edgar/data/1/2/x.htm",
        "https://www.sec.gov:8080/Archives/edgar/data/1/2/x.htm",
        "https://www.sec.gov/Archives/edgar/data/1/2/x.htm?redirect=elsewhere",
        "file:///etc/passwd",
    ],
)
def test_rejects_non_archive_urls(url):
    with pytest.raises(SECError):
        check_archive_url(url)


def test_accepts_archive_urls():
    url = "https://www.sec.gov/Archives/edgar/data/885725/000094787120000527/ss174648_ex1002.htm"
    assert check_archive_url(url) == url


def test_html_to_text_strips_markup_and_keeps_paragraphs():
    html = (
        "<html><head><title>x</title><style>p{}</style></head><body>"
        "<p>Section 7.1 <b>Leverage</b>&nbsp;Ratio.</p><script>evil()</script>"
        "<table><tr><td>3.50</td><td>to 1.00</td></tr></table></body></html>"
    )
    assert html_to_text(html) == "Section 7.1 Leverage Ratio.\n\n3.50 to 1.00"


@respx.mock
@pytest.mark.anyio
async def test_list_filings_reads_older_pages_only_when_since_reaches_back(client):
    respx.get("https://data.sec.gov/submissions/CIK0000885725.json").mock(
        return_value=httpx.Response(200, json=fixture_json("submissions_bsx.json"))
    )
    page = respx.get("https://data.sec.gov/submissions/CIK0000885725-submissions-001.json").mock(
        return_value=httpx.Response(200, json=fixture_json("submissions_bsx_page001.json"))
    )

    recent = await list_filings(client, 885725, ["10-K"], since=date(2025, 1, 1))
    assert page.call_count == 0
    assert recent and all(f["form"] == "10-K" and f["filed"] >= "2025-01-01" for f in recent)

    everything = await list_filings(client, 885725, ["10-k", "8-K"], since=date(2011, 1, 1))
    assert page.call_count == 1
    assert len(everything) > len(recent)
    assert [f["filed"] for f in everything] == sorted(
        (f["filed"] for f in everything), reverse=True
    )
