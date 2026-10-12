from types import SimpleNamespace

import pytest
from agent_fakes import AGREEMENT_HTML, FakeLLM, good_extraction

from covenant_monitor.agent import graph, nodes
from covenant_monitor.agent.config import AgentConfig, Budget
from covenant_monitor.agent.llm import BudgetExceeded, Ledger
from covenant_monitor.agent.memo import stray_digits, unknown_placeholders
from covenant_monitor.agent.models import (
    Approval,
    CitedThreshold,
    Fact,
    Section,
    UsageRecord,
)
from covenant_monitor.agent.nodes import NODE_WRITES, Deps, check_citations, node
from covenant_monitor.agent.ratios import applicable_threshold, compute
from covenant_monitor.agent.sections import chunk_sections, split_sections
from covenant_monitor.edgar import html_to_text

# ---- each node writes only its own fields ----


def test_every_graph_node_declares_what_it_writes():
    compiled = graph.build_graph(AgentConfig())
    assert set(compiled.get_graph().nodes) - {"__start__", "__end__"} == set(NODE_WRITES)


def test_each_nodes_declared_fields_are_disjoint():
    seen: dict[str, str] = {}
    for name, fields in NODE_WRITES.items():
        for f in fields:
            assert f not in seen, f"{f} written by both {seen[f]} and {name}"
            seen[f] = name


def _runtime(config=None):
    config = config or AgentConfig()
    return SimpleNamespace(context=Deps(FakeLLM(), None, config, Ledger(config)))


@pytest.mark.anyio
async def test_a_node_writing_another_nodes_field_is_an_error():
    @node("compute_ratios")
    async def rogue(state, deps, meter):
        return {"ratios": [], "memo": "sneaky"}

    with pytest.raises(RuntimeError, match=r"does not own: \['memo'\]"):
        await rogue({}, _runtime())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_nodes_skip_once_the_run_has_stopped():
    assert await nodes.compute_ratios({"stopped": "earlier failure"}, _runtime()) == {}


# ---- budgets ----


def test_ledger_resumes_from_recorded_usage():
    config = AgentConfig()
    config.budgets["review"] = Budget(max_calls=2, max_tokens=10_000)
    ledger = Ledger(config)
    ledger.seed([UsageRecord(node="review", model="m", input_tokens=10, output_tokens=1)] * 2)
    with pytest.raises(BudgetExceeded, match="used all 2 model calls"):
        ledger.meter("review").before_call()


# ---- ratio math ----


def _fact(concept, value):
    return Fact(
        concept=concept,
        value=value,
        period_start=None,
        period_end="2025-12-31",
        accession="0000000001-26-000010",
    )


FACTS = {
    "OperatingIncomeLoss": _fact("OperatingIncomeLoss", 800),
    "DepreciationDepletionAndAmortization": _fact("DepreciationDepletionAndAmortization", 200),
    "InterestExpense": _fact("InterestExpense", 100),
    "LongTermDebt": _fact("LongTermDebt", 3000),
    "CashAndCashEquivalentsAtCarryingValue": _fact("CashAndCashEquivalentsAtCarryingValue", 500),
}


def test_step_downs_use_the_level_in_effect_at_period_end():
    cov = good_extraction().covenants[0]
    cov.thresholds = [
        CitedThreshold(ratio=4.0, effective_from=None, section="7.1", quote="q"),
        CitedThreshold(ratio=3.5, effective_from="2025-06-30", section="7.1", quote="q"),
        CitedThreshold(ratio=3.0, effective_from="2026-06-30", section="7.1", quote="q"),
    ]
    assert applicable_threshold(cov, "2025-12-31") == 3.5
    assert applicable_threshold(cov, "2025-03-31") == 4.0


def test_leverage_net_leverage_and_coverage():
    cov = good_extraction().covenants[0]
    r = compute(0, cov, FACTS)
    assert (r.ratio, r.threshold, r.headroom, r.headroom_pct) == (3.0, 3.75, 0.75, 0.2)
    assert set(r.inputs) == {
        "LongTermDebt",
        "OperatingIncomeLoss",
        "DepreciationDepletionAndAmortization",
    }

    cov.numerator.value = "net_debt"
    assert compute(0, cov, FACTS).ratio == 2.5

    cov.type.value, cov.numerator.value, cov.denominator.value = (
        "min_interest_coverage",
        "ebitda",
        "interest_expense",
    )
    cov.thresholds[0].ratio = 3.0
    r = compute(0, cov, FACTS)
    assert (r.ratio, r.headroom) == (10.0, 7.0)  # coverage: headroom is ratio minus minimum


def test_noncurrent_plus_debt_current_without_double_counting():
    facts = {k: v for k, v in FACTS.items() if k != "LongTermDebt"}
    facts["LongTermDebtAndCapitalLeaseObligations"] = _fact(
        "LongTermDebtAndCapitalLeaseObligations", 2700
    )
    facts["DebtCurrent"] = _fact("DebtCurrent", 300)
    facts["ShortTermBorrowings"] = _fact("ShortTermBorrowings", 300)  # inside DebtCurrent
    r = compute(0, good_extraction().covenants[0], facts)
    assert r.numerator == 3000 and "ShortTermBorrowings" not in r.inputs


def test_uncomputable_covenants_say_why():
    cov = good_extraction().covenants[0]
    cov.numerator.value = "secured_debt"
    r = compute(0, cov, FACTS)
    assert not r.computable and "secured debt is not available" in r.reason

    cov = good_extraction().covenants[0]
    r = compute(
        0,
        cov,
        {
            k: v
            for k, v in FACTS.items()
            if k != "InterestExpense" and k != "DepreciationDepletionAndAmortization"
        },
    )
    assert not r.computable and "EBITDA needs" in r.reason


def test_analyst_threshold_override_is_marked():
    r = compute(
        0,
        good_extraction().covenants[0],
        FACTS,
        Approval(decision="edit", threshold_overrides={0: 4.0}),
    )
    assert r.threshold == 4.0 and r.threshold_overridden


# ---- sections and citations ----


def test_split_keeps_body_over_table_of_contents():
    sections = {s.id: s for s in split_sections(html_to_text(AGREEMENT_HTML))}
    assert set(sections) == {"1.1", "7.1", "7.2"}
    assert "to exceed 3.75 to 1.00" in sections["7.1"].text


def test_long_definition_sections_split_per_term():
    filler = "x " * 12_000
    text = (
        f"1.1. Defined Terms.\n“Consolidated EBITDA” means {filler}\n"
        f"“Leverage Ratio” means debt to EBITDA.\n7.1. Financial Covenant.\nShall not exceed."
    )
    ids = [s.id for s in split_sections(text)]
    assert ids == ["1.1", "1.1 “Consolidated EBITDA”", "1.1 “Leverage Ratio”", "7.1"]


def test_chunks_respect_the_size_limit():
    sections = [Section(id=str(i), heading="h", text="x" * 400) for i in range(10)]
    chunks = chunk_sections(sections, 1000)
    assert [len(c) for c in chunks] == [2, 2, 2, 2, 2]


def test_citation_checks_normalize_quotes_and_name_the_right_section():
    sections = split_sections(html_to_text(AGREEMENT_HTML))
    extraction = good_extraction()
    extraction.covenants[0].numerator.quote = '"Leverage Ratio" means the ratio'  # straight quotes
    assert check_citations(extraction, sections) == []

    extraction.covenants[0].type.section = "1.1"
    issues = check_citations(extraction, sections)
    assert issues == ["c0.type: quote not found verbatim in section '1.1'; it appears in '7.1'"]


# ---- memo rules ----


def test_memo_checks_flag_typed_digits_and_unknown_keys():
    facts = {"company": "3M COMPANY", "c0.ratio": "3.00x"}
    paragraphs = ["3M COMPANY leverage was {c0.ratio}.", "Up from 2.9x, see {c0.typo}."]
    assert stray_digits(paragraphs, facts) == ["2.9x,"]
    assert unknown_placeholders(paragraphs, facts) == ["c0.typo"]


# ---- picking the agreement ----


def test_picks_newest_credit_agreement_and_skips_amendments():
    def hit(filed, exhibit, name):
        return {
            "filed": filed,
            "exhibit_type": exhibit,
            "description": exhibit,
            "url": f"https://www.sec.gov/Archives/edgar/data/1/2/{name}.htm",
        }

    hits = [
        hit("2026-02-26", "EX-10.2", "exhibit102-creditagreement"),
        hit("2026-02-26", "EX-10.1", "exhibit101-creditagreement"),
        hit("2026-05-01", "EX-10.1", "first-amendment-to-credit-agreement"),
        hit("2026-06-01", "EX-99.1", "press-release-credit"),
    ]
    assert nodes._pick_agreement(hits)["url"].endswith("exhibit101-creditagreement.htm")
