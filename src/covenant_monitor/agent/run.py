"""Start or resume a run, with a SQLite checkpointer so runs can pause and resume.

uv run python -m covenant_monitor.agent.run start BSX
uv run python -m covenant_monitor.agent.run resume <thread> approve --note "reviewed"

Set PHOENIX_COLLECTOR_ENDPOINT (e.g. http://localhost:6006) to send traces to Phoenix.
"""

import argparse
import asyncio
import json
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from covenant_monitor import golden
from covenant_monitor.agent import models, tracing
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.graph import build_graph
from covenant_monitor.agent.llm import LLM, AnthropicLLM, Ledger
from covenant_monitor.agent.nodes import Deps
from covenant_monitor.agent.pricing import cost_usd
from covenant_monitor.agent.tools import EdgarTools, stdio_tools
from covenant_monitor.sec_client import user_agent

DB_PATH = Path("data/runs.sqlite")

# Types the checkpointer may rebuild from saved state. LangGraph will refuse unlisted types.
STATE_TYPES = [
    (cls.__module__, cls.__name__)
    for cls in vars(models).values()
    if isinstance(cls, type) and issubclass(cls, BaseModel) and cls.__module__ == models.__name__
] + [(golden.__name__, "CovenantType"), (golden.__name__, "TestFrequency")]


@asynccontextmanager
async def checkpointer(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(db_path)) as conn:
        yield AsyncSqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES))


async def execute(graph, deps: Deps, thread_id: str, graph_input) -> dict:
    """Run until the graph finishes, stops, or pauses for a human. Returns a summary."""
    cfg = {"configurable": {"thread_id": thread_id}}
    resuming = not isinstance(graph_input, dict)
    with tracing.span(
        "covenant_monitor.run",
        "CHAIN",
        session__id=thread_id,  # Phoenix groups a paused run and its resume by session
        input__value=graph_input.resume if resuming else graph_input,
        run__resume=resuming,
    ) as span:
        await graph.ainvoke(graph_input, cfg, context=deps)
        snapshot = await graph.aget_state(cfg)
        tracing.set_attributes(span, output__value=_status(snapshot))
    state = snapshot.values
    pending = [i.value for t in snapshot.tasks for i in t.interrupts]
    status = _status(snapshot)
    return {
        "thread_id": thread_id,
        "status": status,
        "stopped": state.get("stopped"),
        "approval_request": pending[0] if pending else None,
        "memo": state["memo"].markdown if state.get("memo") else None,
        "usage": run_summary(state.get("usage", []), state.get("timings", [])),
    }


def _status(snapshot) -> str:
    state = snapshot.values
    if state.get("stopped"):
        return "stopped"
    if any(t.interrupts for t in snapshot.tasks):
        return "awaiting_approval"
    if state.get("approval") and state["approval"].decision == "reject":
        return "rejected"
    return "done" if state.get("memo") else "incomplete"


def run_summary(usage, timings) -> dict:
    """Per node: model calls, tokens, estimated cost, and seconds spent in the node.

    Seconds add up every execution of a node, so parallel map batches count in full, and
    model seconds are included in node seconds. Totals sit under "totals".
    """
    rows: dict[str, dict] = defaultdict(
        lambda: {
            "calls": 0,
            "tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "model_seconds": 0.0,
            "runs": 0,
            "seconds": 0.0,
            "cost_usd": 0.0,
        }
    )
    for r in usage:
        row = rows[r.node]
        row["calls"] += 1
        row["tokens"] += r.total
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            row[key] += getattr(r, key)
        row["model_seconds"] += r.seconds
        row["cost_usd"] += cost_usd([r])
    for t in timings:
        rows[t.node]["runs"] += 1
        rows[t.node]["seconds"] += t.seconds
    out = {}
    for node, row in rows.items():
        out[node] = {k: round(v, 4) if isinstance(v, float) else v for k, v in row.items()}
    totals = {
        k: round(sum(r[k] for r in out.values()), 4)
        for k in ("calls", "tokens", "cost_usd", "seconds", "model_seconds")
    }
    return {**out, "totals": totals}


def print_summary_table(summary: dict) -> None:
    header = f"{'node':<20}{'calls':>6}{'tokens':>11}{'cost $':>9}{'seconds':>9}"
    print(header)
    print("-" * len(header))
    for node, row in summary.items():
        print(
            f"{node:<20}{row['calls']:>6}{row['tokens']:>11,}{row['cost_usd']:>9.3f}"
            f"{row['seconds']:>9.1f}"
        )


@asynccontextmanager
async def session(
    config: AgentConfig,
    db_path: Path = DB_PATH,
    llm: LLM | None = None,
    tools: EdgarTools | None = None,
):
    async with checkpointer(db_path) as saver:
        graph = build_graph(config, checkpointer=saver)
        if tools is not None:
            yield graph, Deps(llm or AnthropicLLM(), tools, config, Ledger(config))
            return
        async with stdio_tools() as stdio:
            yield graph, Deps(llm or AnthropicLLM(), stdio, config, Ledger(config))


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="covenant_monitor.agent.run")
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("ticker")
    start.add_argument("--agreement-url")
    resume = sub.add_parser("resume")
    resume.add_argument("thread_id")
    resume.add_argument("decision", choices=["approve", "edit", "reject"])
    resume.add_argument("--note", default="")
    resume.add_argument("--threshold", action="append", default=[], metavar="INDEX=VALUE")
    args = parser.parse_args(argv)

    load_dotenv()  # ANTHROPIC_API_KEY for the SDK; the MCP subprocess reads SEC settings itself
    try:
        user_agent()  # fail here with one clear line, not inside the MCP subprocess
    except RuntimeError as e:
        raise SystemExit(str(e)) from None
    config = AgentConfig()
    provider = tracing.setup()
    try:
        async with session(config) as (graph, deps):
            if args.cmd == "start":
                thread_id = uuid.uuid4().hex[:12]
                graph_input = {"ticker": args.ticker.upper(), "agreement_url": args.agreement_url}
            else:
                thread_id = args.thread_id
                overrides = dict(item.split("=", 1) for item in args.threshold)
                graph_input = Command(
                    resume={
                        "decision": args.decision,
                        "note": args.note,
                        "threshold_overrides": overrides,
                    }
                )
            summary = await execute(graph, deps, thread_id, graph_input)
    finally:
        if provider:
            provider.shutdown()  # flush spans to Phoenix

    memo_text = summary.pop("memo")
    usage = summary.pop("usage")
    print(json.dumps(summary, indent=2, default=str))
    print()
    print_summary_table(usage)
    if memo_text:
        print("\n" + memo_text)


if __name__ == "__main__":
    asyncio.run(main())
