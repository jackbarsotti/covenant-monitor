"""Start or resume a run, with a SQLite checkpointer so runs can pause and resume.

uv run python -m covenant_monitor.agent.run start BSX
uv run python -m covenant_monitor.agent.run resume <thread> approve --note "reviewed"
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
from covenant_monitor.agent import models
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.graph import build_graph
from covenant_monitor.agent.llm import LLM, AnthropicLLM, Ledger
from covenant_monitor.agent.nodes import Deps
from covenant_monitor.agent.tools import EdgarTools, stdio_tools

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
    await graph.ainvoke(graph_input, cfg, context=deps)
    snapshot = await graph.aget_state(cfg)
    state = snapshot.values
    pending = [i.value for t in snapshot.tasks for i in t.interrupts]
    if state.get("stopped"):
        status = "stopped"
    elif pending:
        status = "awaiting_approval"
    elif state.get("approval") and state["approval"].decision == "reject":
        status = "rejected"
    else:
        status = "done" if state.get("memo") else "incomplete"
    return {
        "thread_id": thread_id,
        "status": status,
        "stopped": state.get("stopped"),
        "approval_request": pending[0] if pending else None,
        "memo": state["memo"].markdown if state.get("memo") else None,
        "usage": usage_by_node(state.get("usage", [])),
    }


def usage_by_node(records) -> dict:
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in records:
        out[r.node]["calls"] += 1
        out[r.node]["tokens"] += r.total
    return {k: dict(v) for k, v in out.items()}


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
    config = AgentConfig()
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

    memo_text = summary.pop("memo")
    print(json.dumps(summary, indent=2, default=str))
    if memo_text:
        print("\n" + memo_text)


if __name__ == "__main__":
    asyncio.run(main())
