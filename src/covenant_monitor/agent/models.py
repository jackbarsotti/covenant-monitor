"""Typed data that flows through the graph.

The extraction models double as the structured-output schemas the model fills in, so they
use plain JSON types (ISO date strings, no numeric constraints). Code checks the rest.
"""

from typing import Literal

from pydantic import BaseModel

from covenant_monitor.golden import CovenantType, TestFrequency

Component = Literal["total_debt", "net_debt", "secured_debt", "ebitda", "interest_expense", "other"]


class Company(BaseModel):
    cik: int
    name: str
    ticker: str
    exchange: str | None = None


class AgreementDoc(BaseModel):
    accession: str
    exhibit_type: str
    description: str | None
    filed: str
    url: str
    text: str


class Section(BaseModel):
    id: str
    heading: str
    text: str


class Fact(BaseModel):
    concept: str
    value: float
    period_start: str | None
    period_end: str
    accession: str


class Financials(BaseModel):
    facts: dict[str, Fact]


# ---- Extraction: every field carries the section it came from and a verbatim quote ----


class CitedType(BaseModel):
    value: CovenantType
    section: str
    quote: str


class CitedThreshold(BaseModel):
    ratio: float
    effective_from: str | None  # ISO date; null for the level that applies from closing
    section: str
    quote: str


class CitedFrequency(BaseModel):
    value: TestFrequency
    section: str
    quote: str


class CitedComponent(BaseModel):
    value: Component
    section: str
    quote: str


class ExtractedCovenant(BaseModel):
    name: str
    type: CitedType
    thresholds: list[CitedThreshold]
    test_frequency: CitedFrequency
    numerator: CitedComponent
    denominator: CitedComponent


class ExtractedAddBack(BaseModel):
    name: str
    cap: str | None
    section: str
    quote: str


class Extraction(BaseModel):
    covenants: list[ExtractedCovenant]
    ebitda_addbacks: list[ExtractedAddBack]


class SectionRelevance(BaseModel):
    relevant_section_ids: list[str]


# ---- Review ----


class FieldVerdict(BaseModel):
    field_id: str
    ok: bool
    issue: str | None


class ReviewVerdicts(BaseModel):
    verdicts: list[FieldVerdict]


class ReviewResult(BaseModel):
    passed: bool
    issues: list[str]
    attempt: int


# ---- Ratios, approval, memo ----


class RatioResult(BaseModel):
    covenant_index: int
    name: str
    type: CovenantType
    computable: bool
    reason: str | None = None
    ratio: float | None = None
    threshold: float | None = None
    headroom: float | None = None
    headroom_pct: float | None = None
    period_end: str | None = None
    numerator: float | None = None
    denominator: float | None = None
    inputs: dict[str, Fact] = {}
    threshold_overridden: bool = False


class Approval(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    note: str = ""
    # covenant index -> threshold the analyst wants applied instead of the extracted one
    threshold_overrides: dict[int, float] = {}


class MemoDraft(BaseModel):
    title: str
    paragraphs: list[str]


class Memo(BaseModel):
    markdown: str
    facts: dict[str, str]


class UsageRecord(BaseModel):
    node: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
