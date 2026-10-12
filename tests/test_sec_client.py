import asyncio

import httpx
import pytest
import respx

from covenant_monitor.sec_client import (
    DAY,
    NotFoundError,
    RateLimiter,
    SECError,
    retry_after_seconds,
    user_agent,
)

pytestmark = pytest.mark.anyio

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000885725.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/885725/000094787120000527/ex.htm"


async def test_limiter_never_allows_more_than_9_per_second_under_concurrency(clock):
    limiter = RateLimiter(limit=9, clock=clock, sleep=clock.sleep)
    times: list[float] = []

    async def call():
        await limiter.acquire()
        times.append(clock())

    await asyncio.gather(*(call() for _ in range(40)))

    times.sort()
    assert len(times) == 40
    for t in times:
        assert sum(1 for u in times if t <= u < t + 1.0) <= 9
    # 40 calls at 9 per second need at least 4 full seconds, and shouldn't take much more.
    assert 4.0 <= times[-1] - times[0] < 5.0


@respx.mock
async def test_429_then_200_waits_for_retry_after(client, clock):
    route = respx.get(FACTS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await client.get_json(FACTS_URL) == {"ok": True}
    assert route.call_count == 2
    assert 7.0 in clock.sleeps


@respx.mock
async def test_404_does_not_retry(client):
    route = respx.get(FACTS_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError):
        await client.get_json(FACTS_URL)
    assert route.call_count == 1


@respx.mock
async def test_other_4xx_does_not_retry(client):
    route = respx.get(FACTS_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(SECError, match="403"):
        await client.get_json(FACTS_URL)
    assert route.call_count == 1


@respx.mock
async def test_5xx_and_timeouts_retry_with_backoff_then_give_up(client, clock):
    route = respx.get(FACTS_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ReadTimeout("slow"),
            httpx.Response(500),
            httpx.Response(502),
        ]
    )
    with pytest.raises(SECError, match="after 4 attempts"):
        await client.get_json(FACTS_URL)
    assert route.call_count == 4
    # jitter is pinned at 0.5, so backoff is exactly 1, 2, 4 seconds
    assert clock.sleeps == [1.0, 2.0, 4.0]


@respx.mock
async def test_cache_hit_makes_no_second_request(client):
    route = respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json={"n": 1}))
    assert await client.get_json(FACTS_URL) == {"n": 1}
    assert await client.get_json(FACTS_URL) == {"n": 1}
    assert route.call_count == 1


@respx.mock
async def test_company_facts_cache_expires_after_a_day(client, clock):
    route = respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json={"n": 1}))
    await client.get_json(FACTS_URL)
    clock.now += DAY + 1
    await client.get_json(FACTS_URL)
    assert route.call_count == 2


@respx.mock
async def test_archived_documents_never_expire(client, clock):
    route = respx.get(DOC_URL).mock(return_value=httpx.Response(200, text="<p>hi</p>"))
    await client.get_text(DOC_URL)
    clock.now += 365 * DAY
    await client.get_text(DOC_URL)
    assert route.call_count == 1


@respx.mock
async def test_sends_user_agent(client):
    route = respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json={}))
    await client.get_json(FACTS_URL)
    assert route.calls.last.request.headers["User-Agent"] == "Jane Doe jane@example.com"


def test_retry_after_accepts_seconds_and_http_dates():
    assert retry_after_seconds("3", now=0) == 3.0
    assert retry_after_seconds("Thu, 01 Jan 1970 00:00:10 GMT", now=4) == 6.0
    assert retry_after_seconds("garbage", now=0) is None
    assert retry_after_seconds(None, now=0) is None


def test_user_agent_requires_name_and_email(monkeypatch):
    monkeypatch.setattr("covenant_monitor.sec_client.load_dotenv", lambda: None)
    for bad in ("", "no-email", "jane@example.com", "Jane Doe"):
        monkeypatch.setenv("SEC_USER_AGENT", bad)
        with pytest.raises(RuntimeError):
            user_agent()
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe jane@example.com")
    assert user_agent() == "Jane Doe jane@example.com"
