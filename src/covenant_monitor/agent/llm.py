"""Model calls, with per-node budgets.

Every call goes through a Meter for the node making it. The meter refuses a call once the
node has used its call allowance, and stops the run once the node's tokens pass its cap.
Usage is also written into graph state, so a resumed run picks up where the totals were.
"""

import time
from collections import defaultdict
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from covenant_monitor.agent import tracing
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.models import UsageRecord

T = TypeVar("T", bound=BaseModel)

# Server-side refusal fallbacks route by refusal category; Haiku has none.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
MAX_TOKENS = 16_000


class AgentStop(Exception):
    """Stops the run. The message is the reason shown to the user."""


class BudgetExceeded(AgentStop):
    pass


class Ledger:
    """Running totals per node for one process. Seeded from state after a resume."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.calls: dict[str, int] = defaultdict(int)
        self.tokens: dict[str, int] = defaultdict(int)
        self._seeded = False

    def seed(self, records: list[UsageRecord]) -> None:
        if self._seeded:
            return
        self._seeded = True
        for r in records:
            self.calls[r.node] += 1
            self.tokens[r.node] += r.total

    def meter(self, node: str) -> "Meter":
        return Meter(self, node)


class Meter:
    def __init__(self, ledger: Ledger, node: str):
        self.ledger = ledger
        self.node = node
        self.records: list[UsageRecord] = []

    @property
    def budget(self):
        return self.ledger.config.budgets[self.node]

    def before_call(self) -> None:
        used = self.ledger.calls[self.node]
        if used >= self.budget.max_calls:
            raise BudgetExceeded(
                f"{self.node} used all {self.budget.max_calls} model calls in its budget"
            )
        self.ledger.calls[self.node] += 1

    def after_call(self, record: UsageRecord) -> None:
        self.records.append(record)
        self.ledger.tokens[self.node] += record.total
        used = self.ledger.tokens[self.node]
        if used > self.budget.max_tokens:
            raise BudgetExceeded(
                f"{self.node} used {used:,} tokens, over its budget of {self.budget.max_tokens:,}"
            )


class LLM(Protocol):
    async def structured(
        self,
        *,
        meter: Meter,
        model: str,
        system: str,
        prompt: str,
        output: type[T],
        document: str | None = None,
        effort: str | None = None,
    ) -> T: ...


class AnthropicLLM:
    """Raw Anthropic SDK. Structured outputs via messages.parse; long documents cached."""

    def __init__(self, client: anthropic.AsyncAnthropic | None = None):
        self.client = client or anthropic.AsyncAnthropic()

    async def structured(
        self,
        *,
        meter: Meter,
        model: str,
        system: str,
        prompt: str,
        output: type[T],
        document: str | None = None,
        effort: str | None = None,
    ) -> T:
        content = []
        if document is not None:
            # The document comes first and is cached, so retries and follow-up calls on
            # the same agreement text read it from cache.
            content.append(
                {"type": "text", "text": document, "cache_control": {"type": "ephemeral"}}
            )
        content.append({"type": "text", "text": prompt})
        request = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "output_format": output,
        }
        if effort:
            request["output_config"] = {"effort": effort}

        meter.before_call()
        with tracing.span(
            f"llm {meter.node}",
            "LLM",
            llm__provider="anthropic",
            llm__model_name=model,
            llm__invocation_parameters={"effort": effort, "max_tokens": MAX_TOKENS},
            input__value=prompt,
        ) as span:
            started = time.perf_counter()
            if model in FALLBACK_MODELS:
                response = await self.client.beta.messages.parse(
                    **request, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = await self.client.messages.parse(**request)
            seconds = time.perf_counter() - started

            u = response.usage
            record = UsageRecord(
                node=meter.node,
                model=response.model,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read_tokens=u.cache_read_input_tokens or 0,
                cache_write_tokens=u.cache_creation_input_tokens or 0,
                seconds=round(seconds, 3),
            )
            prompt_tokens = (
                record.input_tokens + record.cache_read_tokens + record.cache_write_tokens
            )
            tracing.set_attributes(
                span,
                llm__served_model=response.model,  # differs from model after a fallback
                llm__token_count__prompt=prompt_tokens,
                llm__token_count__completion=record.output_tokens,
                llm__token_count__total=prompt_tokens + record.output_tokens,
                llm__token_count__prompt_details__cache_read=record.cache_read_tokens,
                llm__token_count__prompt_details__cache_write=record.cache_write_tokens,
                llm__stop_reason=response.stop_reason,
                output__value=(
                    response.parsed_output.model_dump_json() if response.parsed_output else None
                ),
            )
            meter.after_call(record)
            if response.stop_reason == "refusal":
                tracing.mark_error(span, "refusal")
                raise AgentStop(f"{meter.node}: the model declined the request")
            if response.stop_reason == "max_tokens" or response.parsed_output is None:
                tracing.mark_error(span, "cut off or unparseable")
                raise AgentStop(f"{meter.node}: the model's answer was cut off or unparseable")
            return response.parsed_output
