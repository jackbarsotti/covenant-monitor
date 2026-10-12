"""End-to-end graph runs on a made-up company: real MCP tools and routing, scripted model."""

import httpx
import pytest
import respx
from agent_fakes import (
    AGREEMENT_HTML,
    AGREEMENT_URL,
    FACTS_URL,
    FakeLLM,
    bad_extraction,
    companyfacts,
    good_extraction,
)
from helpers import fixture_json
from langgraph.types import Command
from mcp import Client

from covenant_monitor.agent.config import AgentConfig, Budget
from covenant_monitor.agent.models import MemoDraft
from covenant_monitor.agent.run import execute, session
from covenant_monitor.agent.tools import EdgarTools
from covenant_monitor.mcp_server.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def edgar():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
            return_value=httpx.Response(200, json=fixture_json("company_tickers_exchange.json"))
        )
        mock.get(AGREEMENT_URL).mock(return_value=httpx.Response(200, text=AGREEMENT_HTML))
        facts = mock.get(FACTS_URL).mock(return_value=httpx.Response(200, json=companyfacts()))
        yield facts


@pytest.fixture
async def tools(client):
    async with Client(build_server(client)) as mcp_client:
        yield EdgarTools(mcp_client)


async def run(tools, tmp_path, llm, graph_input, thread="t1", config=None):
    async with session(config or AgentConfig(), tmp_path / "runs.sqlite", llm, tools) as (g, d):
        return await execute(g, d, thread, graph_input)


START = {"ticker": "BSX", "agreement_url": AGREEMENT_URL}


def tight(facts_route):
    """Debt of 3,500 on EBITDA of 1,000: 3.50x against 3.75x, about 6.7% headroom."""
    facts_route.mock(return_value=httpx.Response(200, json=companyfacts(debt=3500)))


async def test_happy_path_drafts_a_cited_memo(tools, edgar, tmp_path):
    llm = FakeLLM()
    out = await run(tools, tmp_path, llm, START)

    assert out["status"] == "done", out
    memo = out["memo"]
    # total debt 3,000 / EBITDA (800 + 200) = 3.00x against 3.75x
    assert "3.00x against a maximum of 3.75x (7.1)" in memo
    assert "headroom of +0.75x" in memo
    assert "## Sources" in memo and "`c0.ratio`: 3.00x" in memo
    assert set(out["usage"]) == {"map_sections", "extract_covenants", "review", "draft_memo"}
    # models come from config, and the agreement text is passed as the cached document
    assert llm.calls["review"][0]["model"] == "claude-opus-5-5"
    assert "<section id='7.1'>" in llm.calls["extract_covenants"][0]["document"]
    assert "<section id='7.2'>" not in llm.calls["extract_covenants"][0]["document"]


async def test_low_headroom_pauses_then_resumes_from_checkpoint(tools, edgar, tmp_path):
    tight(edgar)
    llm = FakeLLM()
    paused = await run(tools, tmp_path, llm, START)
    assert paused["status"] == "awaiting_approval"
    assert "below the 15% alert threshold" in paused["approval_request"]["reasons"][0]
    assert "draft_memo" not in llm.calls

    # A fresh session on the same database picks up the paused run.
    resumed = await run(
        tools, tmp_path, llm, Command(resume={"decision": "approve", "note": "checked with desk"})
    )
    assert resumed["status"] == "done"
    assert "checked with desk" in llm.calls["draft_memo"][0]["prompt"]
    assert len(llm.calls["extract_covenants"]) == 1  # nothing before the pause re-ran


async def test_reject_ends_without_a_memo(tools, edgar, tmp_path):
    tight(edgar)
    llm = FakeLLM()
    await run(tools, tmp_path, llm, START)
    out = await run(tools, tmp_path, llm, Command(resume={"decision": "reject"}))
    assert out["status"] == "rejected" and out["memo"] is None
    assert "draft_memo" not in llm.calls


async def test_edit_recomputes_with_the_analysts_threshold(tools, edgar, tmp_path):
    tight(edgar)
    llm = FakeLLM()
    await run(tools, tmp_path, llm, START)
    out = await run(
        tools,
        tmp_path,
        llm,
        Command(
            resume={
                "decision": "edit",
                "note": "amendment raised it",
                "threshold_overrides": {"0": 4.0},
            }
        ),
    )
    assert out["status"] == "done"
    assert "3.50x against a maximum of 4.00x" in out["memo"]


async def test_review_failure_retries_extraction_then_goes_to_a_human(tools, edgar, tmp_path):
    llm = FakeLLM(extract_covenants=lambda p, d, n: bad_extraction())
    out = await run(tools, tmp_path, llm, START)

    assert len(llm.calls["extract_covenants"]) == 3  # first try plus 2 retries
    assert "reviewer rejected" in llm.calls["extract_covenants"][1]["prompt"]
    assert "c0.threshold0: quote not found" in llm.calls["extract_covenants"][1]["prompt"]
    assert "review" not in llm.calls  # code checks failed first; the reviewer model never ran
    assert out["status"] == "awaiting_approval"
    assert "review still failing after 3" in out["approval_request"]["reasons"][0]


async def test_retry_that_fixes_the_extraction_continues(tools, edgar, tmp_path):
    llm = FakeLLM(
        extract_covenants=lambda p, d, n: bad_extraction() if n == 1 else good_extraction()
    )
    out = await run(tools, tmp_path, llm, START)
    assert out["status"] == "done"
    assert len(llm.calls["extract_covenants"]) == 2


async def test_over_budget_stops_and_says_why(tools, edgar, tmp_path):
    config = AgentConfig()
    config.budgets["extract_covenants"] = Budget(max_calls=3, max_tokens=500)
    out = await run(tools, tmp_path, FakeLLM(), START, config=config)
    assert out["status"] == "stopped"
    assert "extract_covenants used 1,100 tokens, over its budget of 500" in out["stopped"]
    assert out["usage"]["extract_covenants"]["tokens"] == 1100


async def test_memo_with_untraced_numbers_is_refused(tools, edgar, tmp_path):
    sloppy = MemoDraft(title="Memo", paragraphs=["Leverage was 3.0x against {c0.threshold}."])
    out = await run(tools, tmp_path, FakeLLM(draft_memo=lambda p, d, n: sloppy), START)
    assert out["status"] == "stopped"
    assert "do not trace to a source" in out["stopped"]


async def test_stale_facts_from_older_years_are_not_mixed_in(tools, edgar, tmp_path):
    facts = companyfacts()
    old = facts["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"][0]
    old.update(end="2017-12-31", filed="2018-02-20")  # company stopped tagging it
    edgar.mock(return_value=httpx.Response(200, json=facts))
    out = await run(tools, tmp_path, FakeLLM(), START)
    assert out["status"] == "awaiting_approval"
    assert (
        "no long-term debt reported for this fiscal year" in out["approval_request"]["reasons"][0]
    )


async def test_tool_errors_stop_the_run_with_the_tools_message(tools, edgar, tmp_path):
    out = await run(tools, tmp_path, FakeLLM(), {"ticker": "ZZZZ"})
    assert out["status"] == "stopped"
    assert "ZZZZ not found" in out["stopped"]
