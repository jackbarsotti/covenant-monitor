import httpx
import pytest
import respx

from covenant_monitor.edgar import (
    find_cik,
    get_json,
    latest_annual_value,
    pad_cik,
    user_agent,
)


def _facts(rows):
    return {"facts": {"us-gaap": {"OperatingIncomeLoss": {"units": {"USD": rows}}}}}


def _row(start, end, val, filed, form="10-K"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def test_pad_cik():
    assert pad_cik(320193) == "0000320193"
    assert pad_cik("0000320193") == "0000320193"


def test_find_cik_is_case_insensitive():
    tickers = {"0": {"cik_str": 885725, "ticker": "BSX", "title": "Boston Scientific"}}
    assert find_cik(tickers, "bsx") == 885725
    with pytest.raises(KeyError):
        find_cik(tickers, "NOPE")


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


def test_user_agent_requires_name_and_email(monkeypatch):
    monkeypatch.setattr("covenant_monitor.edgar.load_dotenv", lambda: None)
    monkeypatch.setenv("SEC_USER_AGENT", "no-email")
    with pytest.raises(RuntimeError):
        user_agent()
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe jane@example.com")
    assert user_agent() == "Jane Doe jane@example.com"


@respx.mock
def test_get_json_sends_user_agent():
    route = respx.get("https://data.sec.gov/test.json").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with httpx.Client(headers={"User-Agent": "Jane Doe jane@example.com"}) as client:
        assert get_json(client, "https://data.sec.gov/test.json") == {"ok": True}
    assert route.calls.last.request.headers["User-Agent"] == "Jane Doe jane@example.com"
