"""Eval runner and scoring, on the made-up agent fixture. No network, scripted model."""

import json

import httpx
import pytest
import respx
from agent_fakes import (
    AGREEMENT_HTML,
    AGREEMENT_URL,
    FACTS_URL,
    FakeLLM,
    companyfacts,
    good_extraction,
)
from helpers import fixture_json
from mcp import Client

from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.qa import QAAnswer
from covenant_monitor.agent.tools import EdgarTools
from covenant_monitor.evals import judge
from covenant_monitor.evals.judge import CalibrationCase, JudgeVerdict
from covenant_monitor.evals.runner import compare_to_baseline, run_evals
from covenant_monitor.evals.scoring import (
    category,
    golden_threshold,
    match_addbacks,
    same_schedule,
    section_number,
)
from covenant_monitor.golden import GoldenCompany, ThresholdStep
from covenant_monitor.golden import TestFrequency as Frequency
from covenant_monitor.mcp_server.server import build_server

GOLDEN = GoldenCompany.model_validate(
    {
        "provenance": "hand",
        "company": {"ticker": "BSX", "cik": 885725, "name": "Example Corp"},
        "agreement": {
            "accession": "0000000001-26-000001",
            "exhibit": "EX-10.1",
            "url": AGREEMENT_URL,
            "agreement_date": "2026-01-01",
        },
        "covenants": [
            {
                "type": "max_leverage",
                "description": "Leverage Ratio",
                "thresholds": [{"ratio": 3.75, "effective_from": None}],
                "test_frequency": "quarterly",
                "section": "Section 7.1",
                "quote": "to exceed 3.75 to 1.00",
            }
        ],
        "ebitda_addbacks": [
            {"name": "Restructuring charges", "section": "Section 1.1", "cap": "10%"}
        ],
        "latest_fiscal_year": {
            "period_end": "2025-12-31",
            "inputs": {"ebitda": {"value": 1000, "source": "made up"}},
            "expected": [
                {"covenant": "max_leverage", "ratio": 3.0, "threshold": 3.75, "headroom": 0.75}
            ],
        },
        "questions": [
            {
                "question": "What is the maximum Leverage Ratio?",
                "answer": "3.75",
                "section": "Section 7.1",
            },
            {
                "question": "How is the Leverage Ratio defined?",
                "answer": "debt to EBITDA",
                "section": "Section 1.1",
            },
            {
                "question": "What liens are allowed?",
                "answer": "Permitted Liens",
                "section": "Section 7.2",
            },
            {"question": "What was the actual ratio at year end?", "trap": True},
            {"question": "What rate do the 2030 notes pay?", "trap": True},
        ],
    }
)

ANSWERS = {
    "What is the maximum Leverage Ratio?": ("7.1", "to exceed 3.75 to 1.00"),
    "How is the Leverage Ratio defined?": ("1.1", "the ratio of Consolidated Total Debt"),
    "What liens are allowed?": ("7.2", "except Permitted Liens"),
}


def answer(prompt, document, n):
    question = prompt.removeprefix("Question: ")
    if question in ANSWERS:
        section, quote = ANSWERS[question]
        return QAAnswer(found=True, answer="x", section=section, quote=quote)
    return QAAnswer(found=False, answer=None, section=None, quote=None)


def judge_says(passed):
    return lambda p, d, n: JudgeVerdict(passed=passed, unsupported_claims=[])


CALIBRATION = [
    CalibrationCase(id="good", expected=True, why="", facts={}, memo="m"),
    CalibrationCase(id="bad", expected=False, why="", facts={}, memo="m"),
]


def scripted_judge(prompt, document, n):
    # Calibration calls come first: agree with the expected label (good, bad), then pass memos.
    return JudgeVerdict(passed=n != 2, unsupported_claims=[] if n != 2 else ["planted"])


@pytest.fixture
def edgar():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
            return_value=httpx.Response(200, json=fixture_json("company_tickers_exchange.json"))
        )
        mock.get(AGREEMENT_URL).mock(return_value=httpx.Response(200, text=AGREEMENT_HTML))
        yield mock.get(FACTS_URL).mock(return_value=httpx.Response(200, json=companyfacts()))


@pytest.fixture
async def tools(client):
    async with Client(build_server(client)) as mcp_client:
        yield EdgarTools(mcp_client)


@pytest.mark.anyio
async def test_perfect_run_scores_full_marks(tools, edgar, tmp_path):
    llm = FakeLLM(answer_questions=answer, judge=scripted_judge)
    results = await run_evals(
        AgentConfig(), {"BSX": GOLDEN}, tmp_path / "e.sqlite", llm, tools, CALIBRATION
    )
    bsx = results["companies"]["BSX"]
    failed = [c for c in bsx["checks"] if not c["passed"]]
    assert failed == [], failed
    assert results["judge"]["trusted"] and results["judge"]["agreement"] == 1.0
    assert results["score"] == 1.0
    assert "memo_faithful" in {c["name"] for c in bsx["checks"]}
    assert bsx["cost_usd"] > 0  # fake usage is priced
    assert len(llm.calls["answer_questions"]) == 5
    assert llm.calls["answer_questions"][0]["document"]  # whole agreement passed as document


@pytest.mark.anyio
async def test_wrong_extraction_and_answers_fail_the_right_checks(tools, edgar, tmp_path):
    def wrong(p, d, n):
        e = good_extraction()
        e.covenants[0].test_frequency.value = Frequency.ANNUAL
        e.ebitda_addbacks = []
        return e

    def answers_everything(prompt, document, n):
        return QAAnswer(found=True, answer="made up", section="7.1", quote="to exceed 3.75 to 1.00")

    llm = FakeLLM(
        extract_covenants=wrong, answer_questions=answers_everything, judge=scripted_judge
    )
    results = await run_evals(
        AgentConfig(), {"BSX": GOLDEN}, tmp_path / "e.sqlite", llm, tools, CALIBRATION
    )
    failed = {c["name"] for c in results["companies"]["BSX"]["checks"] if not c["passed"]}
    assert failed == {
        "covenant[max_leverage].frequency",
        "addbacks",
        "question[1]",  # wrong section
        "question[2]",
        "question[3]",  # trap answered
        "question[4]",
    }


@pytest.mark.anyio
async def test_untrusted_judge_verdicts_do_not_count(tools, edgar, tmp_path):
    llm = FakeLLM(answer_questions=answer, judge=judge_says(True))  # passes everything
    results = await run_evals(
        AgentConfig(), {"BSX": GOLDEN}, tmp_path / "e.sqlite", llm, tools, CALIBRATION
    )
    assert results["judge"]["agreement"] == 0.5 and not results["judge"]["trusted"]
    names = {c["name"] for c in results["companies"]["BSX"]["checks"]}
    assert "memo_faithful" not in names


def test_calibration_file_is_balanced_and_loads():
    cases = judge.load_cases()
    assert len(cases) >= 10
    assert sum(c.expected for c in cases) == sum(not c.expected for c in cases)


def test_section_numbers_compare_across_formats():
    assert section_number("Section 8.11(a)") == section_number("8.11") == "8.11"
    assert section_number("Section 7.08") == section_number("7.8")
    assert section_number("1.1 “Consolidated EBITDA”") == "1.1"
    assert section_number(None) is None


def test_addback_matching_compares_sets_on_word_overlap():
    result = match_addbacks(
        ["Restructuring charges", "Non-cash stock compensation expense", "Transaction costs"],
        ["restructuring and integration charges", "stock-based compensation (non-cash)"],
    )
    assert result["matched"] == 2
    assert result["missed"] == ["Transaction costs"]
    assert result["recall"] == pytest.approx(2 / 3) and result["precision"] == 1.0


def test_one_clause_split_into_parts_is_not_penalized():
    golden = ["Contract termination costs, facility closure costs, and startup costs"]
    extracted = ["Contract termination costs", "Costs of closing facilities", "Startup costs"]
    result = match_addbacks(golden, extracted)
    assert result["precision"] == 1.0 and result["recall"] == 1.0


def test_generic_words_alone_do_not_match():
    result = match_addbacks(["Restructuring charges"], ["Other charges and expenses"])
    assert result["recall"] == 0.0 and result["precision"] == 0.0


def test_plurals_match():
    result = match_addbacks(["Net foreign currency losses"], ["Loss on foreign currencies"])
    assert result["recall"] == 1.0


def test_equivalent_schedules_match():
    golden = [(4.75, None), (4.5, "2025-12-31"), (4.25, "2026-03-31"), (3.75, "2026-09-30")]
    agent = [
        (3.75, None),
        (4.75, "2024-12-31"),
        (4.5, "2025-12-31"),
        (4.25, "2026-03-31"),
        (3.75, "2026-09-30"),
    ]
    assert same_schedule(golden, agent)
    assert same_schedule([(4.5, None)], [(4.5, "2025-12-31")])
    assert not same_schedule(golden, [(3.75, None)])
    assert not same_schedule(golden, agent[:-1])  # misses the return to 3.75


def test_golden_threshold_uses_the_level_in_effect():
    from datetime import date

    steps = [
        ThresholdStep(ratio=6.75),
        ThresholdStep(ratio=5.5, effective_from="2025-09-30"),
        ThresholdStep(ratio=4.75, effective_from="2026-09-30"),
    ]
    assert golden_threshold(steps, date(2026, 7, 3)) == 5.5
    assert golden_threshold(steps, date(2025, 3, 31)) == 6.75


def test_categories_group_checks():
    assert category("covenant[max_leverage].ratio") == "ratio"
    assert category("question[3]") == "question"
    assert category("addbacks") == "addbacks"


def test_baseline_comparison(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"score": 0.8}))
    assert compare_to_baseline({"score": 0.8}, path)
    assert not compare_to_baseline({"score": 0.79}, path)
    path.write_text(json.dumps({"score": 0.8, "tolerance": 0.025}))
    assert compare_to_baseline({"score": 0.78}, path)
    assert not compare_to_baseline({"score": 0.77}, path)
    assert compare_to_baseline({"score": 0.1}, tmp_path / "missing.json")
