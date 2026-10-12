"""SEC EDGAR client: ticker lookup and XBRL company facts.

Run it on a ticker to print the company's latest annual operating income. That checks
the User-Agent, the ticker lookup, and the XBRL parsing end to end:

    uv run python -m covenant_monitor.edgar BSX
"""

import os
import sys
from datetime import date

import httpx
from dotenv import load_dotenv

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ANNUAL_FORMS = {"10-K", "10-K/A"}


def pad_cik(cik: int | str) -> str:
    """EDGAR's data APIs want the CIK as a 10-digit, zero-padded string."""
    return str(int(cik)).zfill(10)


def user_agent() -> str:
    """SEC rejects requests without a User-Agent that names a person and an email."""
    load_dotenv()
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if " " not in ua or "@" not in ua:
        raise RuntimeError('Set SEC_USER_AGENT in .env, e.g. "Jane Doe jane@example.com"')
    return ua


def get_json(client: httpx.Client, url: str) -> dict:
    resp = client.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def find_cik(tickers: dict, ticker: str) -> int:
    """company_tickers.json is keyed by row number, each row has cik_str and ticker."""
    for row in tickers.values():
        if row["ticker"].upper() == ticker.upper():
            return int(row["cik_str"])
    raise KeyError(f"Ticker {ticker} not found in SEC ticker map")


def latest_annual_value(facts: dict, concept: str) -> dict | None:
    """Most recent full-year value of a us-gaap concept, as reported in a 10-K.

    A 10-K repeats prior years as comparatives and can include quarterly facts,
    so we keep only roughly one-year periods and take the latest period end.
    If the same period appears in more than one filing (next year's comparative,
    or a 10-K/A), the newest filing wins because it may restate the number.
    """
    usd = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get("USD")
    if not usd:
        return None

    annual = []
    for f in usd:
        if f.get("form") not in ANNUAL_FORMS or "start" not in f:
            continue
        days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
        if 350 <= days <= 380:
            annual.append(f)

    if not annual:
        return None
    return max(annual, key=lambda f: (f["end"], f["filed"]))


def main(ticker: str = "BSX") -> None:
    headers = {"User-Agent": user_agent()}
    with httpx.Client(headers=headers) as client:
        cik = find_cik(get_json(client, TICKERS_URL), ticker)
        facts = get_json(client, COMPANY_FACTS_URL.format(cik=pad_cik(cik)))

    name = facts.get("entityName", "?")
    n_concepts = len(facts.get("facts", {}).get("us-gaap", {}))
    op_inc = latest_annual_value(facts, "OperatingIncomeLoss")

    print(f"{name} (CIK {pad_cik(cik)})")
    print(f"us-gaap concepts reported: {n_concepts}")
    if op_inc:
        print(f"OperatingIncomeLoss, FY ending {op_inc['end']}: ${op_inc['val']:,}")
    else:
        print("OperatingIncomeLoss: not reported as a full-year 10-K value")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "BSX")
