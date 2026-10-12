"""Fixture loaders and a fake clock shared by the tests."""

import asyncio
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
UA = "Jane Doe jane@example.com"


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeClock:
    """Time that only moves when someone sleeps, so tests never wait."""

    def __init__(self, now: float = 1_000_000.0):
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)
