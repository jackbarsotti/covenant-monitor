import pytest
from helpers import UA, FakeClock

from covenant_monitor.sec_client import RateLimiter, SECClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def client(tmp_path, clock):
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    async with SECClient(
        UA,
        cache_dir=tmp_path / "cache",
        limiter=limiter,
        sleep=clock.sleep,
        jitter=lambda: 0.5,
        now=clock,
    ) as c:
        yield c
