"""Spans and run summaries, with an in-memory exporter. No network."""

from types import SimpleNamespace

import httpx
import pytest
import respx
from agent_fakes import AGREEMENT_HTML, AGREEMENT_URL, FACTS_URL, FakeLLM, companyfacts
from helpers import fixture_json
from mcp import Client
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from covenant_monitor.agent import tracing
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.llm import AnthropicLLM, Ledger
from covenant_monitor.agent.models import SectionRelevance
from covenant_monitor.agent.run import execute, session
from covenant_monitor.agent.tools import EdgarTools
from covenant_monitor.mcp_server.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    tracing.setup(SimpleSpanProcessor(exporter))
    yield exporter
    tracing.reset()


@pytest.fixture
def edgar():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
            return_value=httpx.Response(200, json=fixture_json("company_tickers_exchange.json"))
        )
        mock.get(AGREEMENT_URL).mock(return_value=httpx.Response(200, text=AGREEMENT_HTML))
        mock.get(FACTS_URL).mock(return_value=httpx.Response(200, json=companyfacts()))
        yield mock


@pytest.fixture
async def tools(client):
    async with Client(build_server(client)) as mcp_client:
        yield EdgarTools(mcp_client)


async def test_a_run_is_one_trace_with_node_and_tool_spans(spans, tools, edgar, tmp_path):
    async with session(AgentConfig(), tmp_path / "r.sqlite", FakeLLM(), tools) as (graph, deps):
        out = await execute(graph, deps, "t1", {"ticker": "BSX", "agreement_url": AGREEMENT_URL})
    assert out["status"] == "done"

    finished = spans.get_finished_spans()
    root = [s for s in finished if s.name == "covenant_monitor.run"]
    assert len(root) == 1
    assert root[0].attributes["session.id"] == "t1"
    assert root[0].attributes["output.value"] == "done"
    assert {s.context.trace_id for s in finished} == {root[0].context.trace_id}

    kinds = {s.name: s.attributes["openinference.span.kind"] for s in finished}
    for node in ("resolve_company", "fetch_agreement", "map_sections", "review", "draft_memo"):
        assert kinds[node] == "CHAIN"
    assert kinds["tool lookup_company"] == "TOOL"
    assert kinds["tool get_document_text"] == "TOOL"

    resolve = next(s for s in finished if s.name == "resolve_company")
    tool = next(s for s in finished if s.name == "tool lookup_company")
    assert tool.parent.span_id == resolve.context.span_id  # tool calls nest under their node


async def test_stopped_nodes_are_marked_as_errors(spans, tools, edgar, tmp_path):
    async with session(AgentConfig(), tmp_path / "r.sqlite", FakeLLM(), tools) as (graph, deps):
        out = await execute(graph, deps, "t2", {"ticker": "ZZZZ"})
    assert out["status"] == "stopped"
    finished = {s.name: s for s in spans.get_finished_spans()}
    assert not finished["resolve_company"].status.is_ok
    assert not finished["tool lookup_company"].status.is_ok


async def test_llm_spans_carry_model_tokens_and_output(spans):
    usage = SimpleNamespace(
        input_tokens=1200,
        output_tokens=80,
        cache_read_input_tokens=5000,
        cache_creation_input_tokens=0,
    )
    response = SimpleNamespace(
        model="claude-haiku-5-5",
        usage=usage,
        stop_reason="end_turn",
        parsed_output=SectionRelevance(relevant_section_ids=["7.1"]),
    )
    calls = []

    async def parse(**kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    meter = Ledger(AgentConfig()).meter("map_sections")
    out = await AnthropicLLM(client).structured(
        meter=meter, model="claude-haiku-5-5", system="s", prompt="p", output=SectionRelevance
    )
    assert out.relevant_section_ids == ["7.1"]
    assert "fallbacks" not in calls[0]  # Haiku has no server-side fallback
    assert meter.records[0].seconds >= 0

    (span,) = spans.get_finished_spans()
    a = span.attributes
    assert a["openinference.span.kind"] == "LLM"
    assert a["llm.model_name"] == "claude-haiku-5-5"
    assert a["llm.token_count.prompt"] == 6200  # input + cache read + cache write
    assert a["llm.token_count.completion"] == 80
    assert a["llm.token_count.prompt_details.cache_read"] == 5000
    assert '"7.1"' in a["output.value"]


async def test_tracing_is_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv(tracing.ENDPOINT_ENV, raising=False)
    assert tracing.setup() is None
