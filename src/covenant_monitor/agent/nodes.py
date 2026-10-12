"""Graph nodes. Each node writes only the state fields listed in NODE_WRITES."""

import json
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, TypedDict

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from covenant_monitor.agent import memo, prompts, ratios
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.llm import LLM, AgentStop, Ledger, Meter
from covenant_monitor.agent.models import (
    AgreementDoc,
    Approval,
    Company,
    Extraction,
    Fact,
    Financials,
    Memo,
    MemoDraft,
    RatioResult,
    ReviewResult,
    ReviewVerdicts,
    Section,
    SectionRelevance,
    UsageRecord,
)
from covenant_monitor.agent.sections import (
    normalize,
    render_sections,
    split_sections,
)
from covenant_monitor.agent.tools import EdgarTools


def _first_stop(a: str | None, b: str | None) -> str | None:
    return a or b


def _concat(a: list, b: list) -> list:
    return a + b


class State(TypedDict, total=False):
    # inputs
    ticker: str
    agreement_url: str | None
    # node outputs
    company: Company
    agreement: AgreementDoc
    sections: list[Section]
    financials: Financials
    relevant_section_ids: Annotated[list[str], _concat]
    extraction: Extraction
    extraction_attempts: int
    review: ReviewResult
    ratios: list[RatioResult]
    approval: Approval
    memo: Memo
    # bookkeeping any node may write
    usage: Annotated[list[UsageRecord], _concat]
    stopped: Annotated[str | None, _first_stop]


NODE_WRITES: dict[str, set[str]] = {
    "resolve_company": {"company"},
    "fetch_agreement": {"agreement", "sections"},
    "fetch_financials": {"financials"},
    "prepare_map": set(),
    "map_sections": {"relevant_section_ids"},
    "extract_covenants": {"extraction", "extraction_attempts"},
    "review": {"review"},
    "compute_ratios": {"ratios"},
    "human_approval": {"approval"},
    "draft_memo": {"memo"},
}
ALWAYS_WRITABLE = {"usage", "stopped"}


@dataclass
class Deps:
    llm: LLM
    tools: EdgarTools
    config: AgentConfig
    ledger: Ledger


def node(name: str):
    """Wraps a node: budget meter, stop handling, usage recording, and the write check."""

    def wrap(fn):
        async def run(state: dict, runtime: Runtime[Deps]) -> dict:
            if state.get("stopped"):
                return {}
            deps = runtime.context
            deps.ledger.seed(state.get("usage", []))
            meter = deps.ledger.meter(name) if name in deps.config.budgets else None
            try:
                out = await fn(state, deps, meter)
            except AgentStop as e:
                out = {"stopped": str(e)}
            if meter and meter.records:
                out = {**out, "usage": meter.records}
            extra = set(out) - NODE_WRITES[name] - ALWAYS_WRITABLE
            if extra:
                raise RuntimeError(f"{name} wrote fields it does not own: {sorted(extra)}")
            return out

        run.__name__ = name
        return run

    return wrap


# ---- data gathering (code) ----


@node("resolve_company")
async def resolve_company(state, deps: Deps, meter):
    return {"company": Company(**await deps.tools.call("lookup_company", ticker=state["ticker"]))}


def _pick_agreement(hits: list[dict]) -> dict:
    """Newest credit agreement exhibit, skipping amendments; lowest exhibit number on ties."""

    def label(h):
        return f"{h.get('description') or ''} {h['url'].rsplit('/', 1)[-1]}".lower()

    candidates = [
        h
        for h in hits
        if h["exhibit_type"].startswith("EX-10")
        and "credit" in label(h)
        and "amend" not in label(h)
    ]
    if not candidates:
        raise AgentStop("no credit agreement exhibit found in EDGAR full-text search")
    newest = max(h["filed"] for h in candidates)
    return min((h for h in candidates if h["filed"] == newest), key=lambda h: h["exhibit_type"])


@node("fetch_agreement")
async def fetch_agreement(state, deps: Deps, meter):
    company: Company = state["company"]
    if state.get("agreement_url"):
        url = state["agreement_url"]
        accession_dir = url.rstrip("/").split("/")[-2]
        hit = {
            "accession": f"{accession_dir[:10]}-{accession_dir[10:12]}-{accession_dir[12:]}",
            "exhibit_type": "as given",
            "description": None,
            "filed": "",
            "url": url,
        }
    else:
        found = await deps.tools.call(
            "search_filings_text",
            cik=company.cik,
            query='"credit agreement"',
            form_types=["8-K", "10-K", "10-Q"],
        )
        hit = _pick_agreement(found["hits"])

    parts, offset = [], 0
    for _ in range(deps.config.max_document_pages):
        page = await deps.tools.call("get_document_text", url=hit["url"], offset=offset)
        parts.append(page["text"])
        offset = page["next_offset"]
        if offset is None:
            break
    else:
        raise AgentStop(f"agreement is longer than {deps.config.max_document_pages} pages")

    agreement = AgreementDoc(
        accession=hit["accession"],
        exhibit_type=hit["exhibit_type"],
        description=hit.get("description"),
        filed=hit["filed"],
        url=hit["url"],
        text="".join(parts),
    )
    return {"agreement": agreement, "sections": split_sections(agreement.text)}


@node("fetch_financials")
async def fetch_financials(state, deps: Deps, meter):
    company: Company = state["company"]
    result = await deps.tools.call(
        "get_annual_financials", cik=company.cik, concepts=ratios.CONCEPTS
    )
    facts = {
        concept: Fact(concept=concept, **v)
        for concept, v in result["values"].items()
        if v is not None
    }
    if not facts:
        raise AgentStop("no annual XBRL financials found for this company")
    # Concepts a company stopped tagging still return their last value, possibly years
    # old. Keep only facts for the latest fiscal year end so a ratio never mixes years.
    year_end = (
        facts.get("OperatingIncomeLoss") or max(facts.values(), key=lambda f: f.period_end)
    ).period_end
    current = {k: f for k, f in facts.items() if f.period_end == year_end}
    return {"financials": Financials(facts=current)}


@node("prepare_map")
async def prepare_map(state, deps: Deps, meter):
    if not state.get("sections"):
        raise AgentStop("could not find numbered sections in the agreement text")
    return {}


# ---- extraction (models) ----


@node("map_sections")
async def map_sections(payload, deps: Deps, meter: Meter):
    chunk: list[Section] = payload["sections"]
    result = await deps.llm.structured(
        meter=meter,
        model=deps.config.models.map_sections,
        system=prompts.MAP,
        prompt=render_sections(chunk, max_chars=deps.config.map_chunk_chars),
        output=SectionRelevance,
        effort="low",
    )
    known = {s.id for s in chunk}
    return {"relevant_section_ids": [i for i in result.relevant_section_ids if i in known]}


def relevant_sections(state) -> list[Section]:
    wanted = set(state.get("relevant_section_ids", []))
    return [s for s in state["sections"] if s.id in wanted]


@node("extract_covenants")
async def extract_covenants(state, deps: Deps, meter: Meter):
    sections = relevant_sections(state)
    if not sections:
        raise AgentStop("no covenant-related sections were found in the agreement")
    attempt = state.get("extraction_attempts", 0) + 1
    prompt = "Extract the financial covenants from the sections above."
    review: ReviewResult | None = state.get("review")
    if review and not review.passed:
        issues = "\n".join(f"- {i}" for i in review.issues)
        prompt += f"\n\nA reviewer rejected the previous extraction:\n{issues}\nFix these."
    extraction = await deps.llm.structured(
        meter=meter,
        model=deps.config.models.extract_covenants,
        system=prompts.EXTRACT,
        document=render_sections(sections),
        prompt=prompt,
        output=Extraction,
        effort="high",
    )
    return {"extraction": extraction, "extraction_attempts": attempt}


def review_fields(extraction: Extraction) -> list[dict[str, Any]]:
    """Every cited field as {id, value, section, quote}: what the reviewer sees."""
    fields = []
    for i, c in enumerate(extraction.covenants):
        fields.append({"id": f"c{i}.type", "value": c.type.value, **_cite(c.type)})
        for j, t in enumerate(c.thresholds):
            value = f"{t.ratio} effective from {t.effective_from or 'closing'}"
            fields.append({"id": f"c{i}.threshold{j}", "value": value, **_cite(t)})
        freq = c.test_frequency
        fields.append({"id": f"c{i}.test_frequency", "value": freq.value, **_cite(freq)})
        fields.append({"id": f"c{i}.numerator", "value": c.numerator.value, **_cite(c.numerator)})
        fields.append(
            {"id": f"c{i}.denominator", "value": c.denominator.value, **_cite(c.denominator)}
        )
    for k, a in enumerate(extraction.ebitda_addbacks):
        fields.append({"id": f"a{k}", "value": f"{a.name} (cap: {a.cap})", **_cite(a)})
    return fields


def _cite(x) -> dict:
    return {"section": x.section, "quote": x.quote}


def check_citations(extraction: Extraction, sections: list[Section]) -> list[str]:
    """Code checks before any model sees the extraction: quotes must be verbatim."""
    by_id = {s.id: normalize(s.text) for s in sections}
    issues = []
    for f in review_fields(extraction):
        quote = normalize(f["quote"])
        if not quote:
            issues.append(f"{f['id']}: quote is empty")
        elif f["section"] not in by_id:
            issues.append(f"{f['id']}: section {f['section']!r} is not one of the given sections")
        elif quote not in by_id[f["section"]]:
            elsewhere = [sid for sid, text in by_id.items() if quote in text]
            where = f"; it appears in {elsewhere[0]!r}" if elsewhere else ""
            issues.append(f"{f['id']}: quote not found verbatim in section {f['section']!r}{where}")
    for i, c in enumerate(extraction.covenants):
        if not c.thresholds:
            issues.append(f"c{i}: no threshold")
        for j, t in enumerate(c.thresholds):
            if t.ratio <= 0:
                issues.append(f"c{i}.threshold{j}: ratio must be positive")
            if t.effective_from is not None:
                try:
                    date.fromisoformat(t.effective_from)
                except ValueError:
                    issues.append(f"c{i}.threshold{j}: effective_from is not an ISO date")
    return issues


@node("review")
async def review(state, deps: Deps, meter: Meter):
    extraction: Extraction = state["extraction"]
    attempt = state["extraction_attempts"]
    issues = check_citations(extraction, relevant_sections(state))
    if not issues:
        fields = review_fields(extraction)
        verdicts = await deps.llm.structured(
            meter=meter,
            model=deps.config.models.review,
            system=prompts.REVIEW,
            prompt=json.dumps(fields, indent=1),
            output=ReviewVerdicts,
            effort="high",
        )
        by_id = {v.field_id: v for v in verdicts.verdicts}
        for f in fields:
            v = by_id.get(f["id"])
            if v is None:
                issues.append(f"{f['id']}: reviewer returned no verdict")
            elif not v.ok:
                issues.append(f"{f['id']}: {v.issue or 'not supported by its quote'}")
    return {"review": ReviewResult(passed=not issues, issues=issues, attempt=attempt)}


# ---- ratios and approval (code) ----


@node("compute_ratios")
async def compute_ratios(state, deps: Deps, meter):
    extraction: Extraction = state["extraction"]
    facts = state["financials"].facts
    approval = state.get("approval")
    return {
        "ratios": [
            ratios.compute(i, cov, facts, approval) for i, cov in enumerate(extraction.covenants)
        ]
    }


def approval_reasons(state, config: AgentConfig) -> list[str]:
    reasons = []
    review_result: ReviewResult = state["review"]
    if not review_result.passed:
        reasons.append(f"review still failing after {review_result.attempt} extraction attempts")
    if not state["extraction"].covenants:
        reasons.append("no financial covenants were extracted")
    for r in state["ratios"]:
        if not r.computable:
            reasons.append(f"{r.name}: could not compute ({r.reason})")
        elif r.headroom_pct < config.alert_headroom_pct:
            reasons.append(
                f"{r.name}: headroom {r.headroom_pct:.1%} is below the "
                f"{config.alert_headroom_pct:.0%} alert threshold"
            )
    return reasons


@node("human_approval")
async def human_approval(state, deps: Deps, meter):
    decision = interrupt(
        {
            "reasons": approval_reasons(state, deps.config),
            "review_issues": state["review"].issues,
            "ratios": [
                r.model_dump(
                    include={
                        "covenant_index",
                        "name",
                        "ratio",
                        "threshold",
                        "headroom_pct",
                        "computable",
                        "reason",
                    }
                )
                for r in state["ratios"]
            ],
            "resume_with": {
                "decision": "approve | edit | reject",
                "note": "why",
                "threshold_overrides": {"<covenant index>": "<threshold>"},
            },
        }
    )
    return {"approval": Approval.model_validate(decision)}


# ---- memo (model writes prose, code fills numbers) ----


@node("draft_memo")
async def draft_memo(state, deps: Deps, meter: Meter):
    facts = memo.build_facts(
        state["company"], state["agreement"], state["extraction"], state["ratios"]
    )
    approval: Approval | None = state.get("approval")
    if approval:
        facts["approval_decision"] = approval.decision
        facts["approval_note"] = approval.note or "none"
    prompt = "Facts table:\n" + json.dumps(facts, indent=1)
    for _ in range(2):
        draft = await deps.llm.structured(
            meter=meter,
            model=deps.config.models.draft_memo,
            system=prompts.MEMO,
            prompt=prompt,
            output=MemoDraft,
            effort="medium",
        )
        paragraphs = [draft.title, *draft.paragraphs]
        stray = memo.stray_digits(paragraphs, facts)
        unknown = memo.unknown_placeholders(paragraphs, facts)
        if not stray and not unknown:
            markdown = memo.render(draft.title, draft.paragraphs, facts)
            return {"memo": Memo(markdown=markdown, facts=facts)}
        prompt += (
            "\n\nYour previous draft broke the rules. "
            f"Digits typed directly: {stray}. Unknown keys: {unknown}. "
            "Use only placeholders from the table."
        )
    raise AgentStop("memo kept including numbers that do not trace to a source")
