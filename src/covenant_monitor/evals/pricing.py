"""Estimated dollar cost of a run from its usage records.

List prices per million tokens, as of 2026-10: (input, output, cache read). Cache writes are
billed at 1.25x input (5-minute cache). Haiku's long-prompt rate (over 100K) is not modeled.
"""

from covenant_monitor.agent.models import UsageRecord

PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}
FALLBACK_PRICE = (10.00, 50.00, 1.00)  # unknown model (e.g. a refusal fallback): price high


def price(model: str) -> tuple[float, float, float]:
    return next((p for name, p in PRICES.items() if model.startswith(name)), FALLBACK_PRICE)


def cost_usd(records: list[UsageRecord]) -> float:
    total = 0.0
    for r in records:
        inp, out, cache_read = price(r.model)
        total += (
            r.input_tokens * inp
            + r.output_tokens * out
            + r.cache_read_tokens * cache_read
            + r.cache_write_tokens * inp * 1.25
        ) / 1_000_000
    return round(total, 4)
