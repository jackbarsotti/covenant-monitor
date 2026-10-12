"""Shared async HTTP client for every SEC request.

One instance per process. It enforces SEC's fair-access rules (a User-Agent naming a person
and an email, under 10 requests per second), retries transient failures, and caches
responses on disk so repeated runs don't re-download the same filings.
"""

import asyncio
import email.utils
import hashlib
import json
import os
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from dotenv import load_dotenv

MAX_PER_SECOND = 9
MAX_ATTEMPTS = 4
DAY = 24 * 60 * 60
DEFAULT_CACHE_DIR = Path("data/cache")
ARCHIVES_PREFIX = "https://www.sec.gov/Archives/"

Sleep = Callable[[float], Awaitable[None]]


class SECError(Exception):
    """A failure the caller can report as-is: the message is short and has no internals."""


class NotFoundError(SECError):
    pass


def user_agent() -> str:
    """SEC rejects requests without a User-Agent that names a person and an email."""
    load_dotenv()
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not re.fullmatch(r"\S.*\s+\S+@\S+\.\S+", ua):
        raise RuntimeError(
            'SEC_USER_AGENT must be a name and an email, e.g. "Jane Doe jane@example.com". '
            "Set it in .env locally, or as a repository secret in CI."
        )
    return ua


class RateLimiter:
    """Allows at most `limit` acquisitions in any one-second window.

    Keeps the times of the last `limit` acquisitions. A new one waits until the oldest of
    those is a full second old. Waiters queue on a lock, so concurrent callers go in order.
    """

    def __init__(
        self,
        limit: int = MAX_PER_SECOND,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ):
        self.limit = limit
        self.clock = clock
        self.sleep = sleep
        self._recent: deque[float] = deque(maxlen=limit)
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while len(self._recent) == self.limit:
                wait = self._recent[0] + 1.0 - self.clock()
                if wait <= 0:
                    break
                await self.sleep(wait)
            self._recent.append(self.clock())


class DiskCache:
    """Response bodies on disk, keyed by full URL.

    Archived filing documents never change, so they never expire. Everything else
    (submissions, company facts, ticker map, search) is refreshed after a day.
    """

    def __init__(self, root: Path, now: Callable[[], float] = time.time):
        self.root = root
        self.now = now

    def _path(self, url: str) -> Path:
        return self.root / hashlib.sha256(url.encode()).hexdigest()

    def get(self, url: str) -> bytes | None:
        path = self._path(url)
        meta_path = path.with_suffix(".json")
        if not path.exists() or not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text())
        if not url.startswith(ARCHIVES_PREFIX) and self.now() - meta["fetched_at"] > DAY:
            return None
        return path.read_bytes()

    def put(self, url: str, body: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(url)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        meta = {"url": url, "fetched_at": self.now()}
        path.with_suffix(".json").write_text(json.dumps(meta))


def retry_after_seconds(value: str | None, now: float) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date."""
    if not value:
        return None
    if value.strip().isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - now)


class SECClient:
    def __init__(
        self,
        ua: str | None = None,
        *,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        limiter: RateLimiter | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        now: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.http = httpx.AsyncClient(
            headers={"User-Agent": ua or user_agent()},
            timeout=30,
            transport=transport,
        )
        self.cache = DiskCache(cache_dir, now)
        self.limiter = limiter or RateLimiter()
        self.sleep = sleep
        self.jitter = jitter
        self.now = now

    async def __aenter__(self) -> "SECClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def get_bytes(self, url: str, params: dict | None = None) -> bytes:
        full_url = str(httpx.URL(url, params=params))
        cached = self.cache.get(full_url)
        if cached is not None:
            return cached
        body = await self._fetch(full_url)
        self.cache.put(full_url, body)
        return body

    async def get_json(self, url: str, params: dict | None = None) -> dict:
        return json.loads(await self.get_bytes(url, params))

    async def get_text(self, url: str) -> str:
        return (await self.get_bytes(url)).decode("utf-8", errors="replace")

    async def _fetch(self, url: str) -> bytes:
        """GET with retries on 429, 5xx, and timeouts. Other 4xx fail at once."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self.limiter.acquire()
            retry_after = None
            try:
                resp = await self.http.get(url)
            except httpx.TimeoutException:
                problem = "timed out"
            else:
                if resp.status_code == 200:
                    return resp.content
                if resp.status_code == 404:
                    raise NotFoundError(f"SEC has no document at {url}")
                if resp.status_code != 429 and resp.status_code < 500:
                    raise SECError(f"SEC returned HTTP {resp.status_code} for {url}")
                problem = f"returned HTTP {resp.status_code}"
                retry_after = retry_after_seconds(resp.headers.get("Retry-After"), self.now())

            if attempt == MAX_ATTEMPTS:
                raise SECError(f"SEC {problem} for {url} after {MAX_ATTEMPTS} attempts")
            backoff = 2 ** (attempt - 1) * (0.5 + self.jitter())
            await self.sleep(retry_after if retry_after is not None else backoff)
        raise AssertionError("unreachable")
