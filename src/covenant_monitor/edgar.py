"""SEC EDGAR operations: companies, filings, full-text search, documents, XBRL facts.

Every request goes through the shared SECClient. Run it on a ticker to print the
company's latest annual operating income. That checks the User-Agent, the ticker
lookup, and the XBRL parsing end to end:

    uv run python -m covenant_monitor.edgar BSX
"""

import asyncio
import re
import sys
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit

from covenant_monitor.sec_client import SECClient, SECError

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}"
ANNUAL_FORMS = {"10-K", "10-K/A"}
MAX_FILINGS = 100
MAX_SEARCH_HITS = 25
PAGE_CHARS = 20_000


def pad_cik(cik: int | str) -> str:
    """EDGAR's data APIs want the CIK as a 10-digit, zero-padded string."""
    return str(int(cik)).zfill(10)


def archive_url(cik: int, accession: str, filename: str) -> str:
    return ARCHIVE_URL.format(cik=int(cik), accession=accession.replace("-", ""), filename=filename)


def find_company(tickers: dict, ticker: str) -> dict:
    """company_tickers_exchange.json is columnar: a list of field names and rows of values."""
    fields = tickers["fields"]
    for row in tickers["data"]:
        company = dict(zip(fields, row, strict=True))
        if company["ticker"].upper() == ticker.upper():
            return company
    raise SECError(f"Ticker {ticker} not found in SEC's ticker list")


async def lookup_company(client: SECClient, ticker: str) -> dict:
    company = find_company(await client.get_json(TICKERS_URL), ticker)
    return {
        "cik": int(company["cik"]),
        "name": company["name"],
        "ticker": company["ticker"],
        "exchange": company["exchange"],
    }


def _rows(columns: dict) -> list[dict]:
    """Submissions store filings as parallel arrays. Turn them into one dict per filing."""
    keys = list(columns)
    return [dict(zip(keys, values, strict=True)) for values in zip(*columns.values(), strict=True)]


async def list_filings(
    client: SECClient, cik: int, forms: list[str], since: date | None = None
) -> list[dict]:
    """Filings of the given forms, newest first.

    The submissions file holds the most recent filings. Older ones sit in extra pages,
    which we read only when `since` reaches back past what the recent block covers.
    """
    subs = await client.get_json(SUBMISSIONS_URL.format(name=f"CIK{pad_cik(cik)}.json"))
    rows = _rows(subs["filings"]["recent"])
    if since:
        for page in subs["filings"].get("files", []):
            if page["filingTo"] >= since.isoformat():
                rows += _rows(await client.get_json(SUBMISSIONS_URL.format(name=page["name"])))

    wanted = {f.upper() for f in forms}
    found = []
    for row in rows:
        if row["form"] not in wanted or (since and row["filingDate"] < since.isoformat()):
            continue
        found.append(
            {
                "accession": row["accessionNumber"],
                "form": row["form"],
                "filed": row["filingDate"],
                "primary_document_url": archive_url(
                    cik, row["accessionNumber"], row["primaryDocument"]
                ),
            }
        )
    found.sort(key=lambda f: f["filed"], reverse=True)
    return found[:MAX_FILINGS]


async def search_filings_text(client: SECClient, cik: int, query: str, forms: list[str]) -> dict:
    """EDGAR full-text search, one result per matching document.

    Exhibits are often filed by an agent, so the accession prefix may not be the company's
    CIK. The archive path still uses the company's CIK.
    """
    params = {"q": query, "ciks": pad_cik(cik)}
    if forms:
        params["forms"] = ",".join(f.upper() for f in forms)
    result = await client.get_json(FULL_TEXT_SEARCH_URL, params)
    hits = []
    for hit in result["hits"]["hits"][:MAX_SEARCH_HITS]:
        src = hit["_source"]
        accession, filename = hit["_id"].split(":", 1)
        hits.append(
            {
                "accession": accession,
                "form": src["form"],
                "filed": src["file_date"],
                "exhibit_type": src["file_type"],
                "description": src.get("file_description"),
                "url": archive_url(cik, accession, filename),
            }
        )
    return {"total": result["hits"]["total"]["value"], "hits": hits}


def check_archive_url(url: str) -> str:
    """Only filing documents on sec.gov, so the model can't point the server at other hosts."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "www.sec.gov"
        or not parts.path.startswith("/Archives/edgar/data/")
        or ".." in parts.path
        or parts.query
        or parts.fragment
    ):
        raise SECError("URL must start with https://www.sec.gov/Archives/edgar/data/")
    return url


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP_TAGS = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skipping += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def get_document_text(client: SECClient, url: str, offset: int = 0) -> dict:
    """Plain text of a filing document, one page of characters at a time."""
    text = html_to_text(await client.get_text(check_archive_url(url)))
    end = offset + PAGE_CHARS
    return {
        "url": url,
        "offset": offset,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
        "text": text[offset:end],
    }


def latest_annual_value(facts: dict, concept: str) -> dict | None:
    """Most recent full-year value of a us-gaap concept, as reported in a 10-K.

    Income statement concepts are durations: a 10-K repeats prior years as comparatives and
    can include quarterly facts, so we keep only roughly one-year periods and take the
    latest period end. Balance sheet concepts (debt, cash) are instants with no start date;
    for those we take the latest year-end value a 10-K reports. If the same period appears
    in more than one filing (next year's comparative, or a 10-K/A), the newest filing wins
    because it may restate the number.
    """
    usd = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get("USD")
    if not usd:
        return None

    annual, instants = [], []
    for f in usd:
        if f.get("form") not in ANNUAL_FORMS:
            continue
        if "start" not in f:
            instants.append(f)
            continue
        days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
        if 350 <= days <= 380:
            annual.append(f)

    candidates = annual or instants
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f["end"], f["filed"]))


async def get_annual_financials(client: SECClient, cik: int, concepts: list[str]) -> dict:
    facts = await client.get_json(COMPANY_FACTS_URL.format(cik=pad_cik(cik)))
    values = {}
    for concept in concepts:
        fact = latest_annual_value(facts, concept)
        values[concept] = fact and {
            "value": fact["val"],
            "period_start": fact.get("start"),
            "period_end": fact["end"],
            "form": fact["form"],
            "filed": fact["filed"],
            "accession": fact["accn"],
        }
    return {"cik": int(cik), "name": facts.get("entityName"), "values": values}


async def main(ticker: str = "BSX") -> None:
    async with SECClient() as client:
        company = await lookup_company(client, ticker)
        result = await get_annual_financials(client, company["cik"], ["OperatingIncomeLoss"])
        facts = await client.get_json(COMPANY_FACTS_URL.format(cik=pad_cik(company["cik"])))

    n_concepts = len(facts.get("facts", {}).get("us-gaap", {}))
    op_inc = result["values"]["OperatingIncomeLoss"]

    print(f"{result['name']} (CIK {pad_cik(company['cik'])})")
    print(f"us-gaap concepts reported: {n_concepts}")
    if op_inc:
        print(f"OperatingIncomeLoss, FY ending {op_inc['period_end']}: ${op_inc['value']:,}")
    else:
        print("OperatingIncomeLoss: not reported as a full-year 10-K value")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "BSX"))
