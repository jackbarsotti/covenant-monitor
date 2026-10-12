"""Golden dataset: schema and loader for the hand-labeled covenant files.

Labels in data/golden/ are written by hand from the source documents. This module only
checks their shape, so a typo is caught before an eval run scores against it. It never
fills in or recomputes a label. Files starting with "_" (the template) are skipped.

    uv run python -m covenant_monitor.golden
"""

import sys
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from covenant_monitor.edgar import check_archive_url
from covenant_monitor.sec_client import SECError

GOLDEN_DIR = Path("data/golden")
QUESTIONS_PER_COMPANY = 5
TRAPS_PER_COMPANY = 2

Accession = Annotated[str, Field(pattern=r"^\d{10}-\d{2}-\d{6}$")]
Section = Annotated[str, Field(min_length=1, description="e.g. Section 7.11(a)")]
Exhibit = Annotated[str, Field(pattern=r"^EX-\d+(\.\d+)*$", description="e.g. EX-10.1")]


def _archive_url(url: str) -> str:
    try:
        return check_archive_url(url)
    except SECError as e:
        raise ValueError(str(e)) from e


ArchiveUrl = Annotated[str, AfterValidator(_archive_url)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CovenantType(StrEnum):
    MAX_LEVERAGE = "max_leverage"
    MAX_SECURED_LEVERAGE = "max_secured_leverage"
    MIN_INTEREST_COVERAGE = "min_interest_coverage"
    MIN_FIXED_CHARGE_COVERAGE = "min_fixed_charge_coverage"
    MIN_NET_WORTH = "min_net_worth"
    OTHER = "other"


class TestFrequency(StrEnum):
    QUARTERLY = "quarterly"
    SEMI_ANNUAL = "semi_annual"
    ANNUAL = "annual"
    SPRINGING = "springing"


class Company(Strict):
    ticker: str = Field(pattern=r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")
    cik: int = Field(ge=1, le=9_999_999_999)
    name: str = Field(min_length=1)


class Amendment(Strict):
    accession: Accession
    exhibit: Exhibit
    url: ArchiveUrl
    amendment_date: date


class AgreementSource(Strict):
    accession: Accession
    exhibit: Exhibit
    url: ArchiveUrl
    agreement_date: date
    amendments: list[Amendment] = []


class ThresholdStep(Strict):
    """One level of the threshold. A step-down schedule is several steps in date order.
    `effective_from` is null for the level that applies from closing."""

    ratio: float = Field(gt=0, description="3.5 means 3.50 to 1.00")
    effective_from: date | None = None


class Covenant(Strict):
    type: CovenantType
    description: str = Field(min_length=1, description="The covenant's name in the agreement")
    thresholds: list[ThresholdStep] = Field(min_length=1)
    test_frequency: TestFrequency
    section: Section
    quote: str = Field(min_length=1, description="Verbatim text from the agreement")

    @model_validator(mode="after")
    def _steps_in_order(self):
        dates = [s.effective_from for s in self.thresholds]
        if dates[0] is not None and len(dates) > 1:
            raise ValueError("the first threshold step should have effective_from: null")
        later = dates[1:]
        if any(d is None for d in later):
            raise ValueError("only the first threshold step may have effective_from: null")
        if later != sorted(later) or len(set(later)) != len(later):
            raise ValueError("threshold steps must be in strictly increasing date order")
        return self


class AddBack(Strict):
    name: str = Field(min_length=1)
    section: Section
    cap: str | None = Field(None, description="Cap as written, e.g. '15% of EBITDA'")


class Input(Strict):
    value: float
    source: str = Field(min_length=1, description="us-gaap concept or where the number came from")
    accession: Accession | None = None


class Expected(Strict):
    """Hand-computed result for one covenant on the latest fiscal year."""

    covenant: CovenantType
    ratio: float
    threshold: float = Field(gt=0)
    headroom: float = Field(description="Signed distance to the threshold, in turns of ratio")


class LatestFiscalYear(Strict):
    period_end: date
    inputs: dict[str, Input] = Field(min_length=1)
    expected: list[Expected] = Field(min_length=1)


class Question(Strict):
    question: str = Field(min_length=1)
    trap: bool = False
    answer: str | None = Field(None, description="null for a trap: not in the agreement")
    section: Section | None = None

    @model_validator(mode="after")
    def _trap_has_no_answer(self):
        if self.trap and (self.answer is not None or self.section is not None):
            raise ValueError("a trap question's answer and section must be null")
        if not self.trap and not self.answer:
            raise ValueError("a non-trap question needs an answer")
        return self


class GoldenCompany(Strict):
    company: Company
    agreement: AgreementSource
    covenants: list[Covenant] = Field(min_length=1)
    ebitda_addbacks: list[AddBack]
    latest_fiscal_year: LatestFiscalYear
    questions: list[Question]

    @model_validator(mode="after")
    def _question_counts(self):
        if len(self.questions) != QUESTIONS_PER_COMPANY:
            raise ValueError(f"need exactly {QUESTIONS_PER_COMPANY} questions")
        traps = sum(q.trap for q in self.questions)
        if traps != TRAPS_PER_COMPANY:
            raise ValueError(f"need exactly {TRAPS_PER_COMPANY} trap questions, found {traps}")
        return self

    @model_validator(mode="after")
    def _expected_matches_covenants(self):
        types = {c.type for c in self.covenants}
        missing = [
            e.covenant.value for e in self.latest_fiscal_year.expected if e.covenant not in types
        ]
        if missing:
            raise ValueError(f"expected results for covenants not listed: {missing}")
        return self


def load_file(path: Path) -> GoldenCompany:
    data = yaml.safe_load(path.read_text())
    golden = GoldenCompany.model_validate(data)
    if path.stem.upper() != golden.company.ticker:
        raise ValueError(f"{path.name}: file name should be {golden.company.ticker}.yaml")
    return golden


def label_files(directory: Path = GOLDEN_DIR) -> list[Path]:
    return sorted(p for p in directory.glob("*.yaml") if not p.name.startswith("_"))


def load_all(directory: Path = GOLDEN_DIR) -> dict[str, GoldenCompany]:
    return {p.stem.upper(): load_file(p) for p in label_files(directory)}


def main() -> None:
    files = label_files()
    failed = 0
    for path in files:
        try:
            golden = load_file(path)
        except (ValidationError, ValueError, yaml.YAMLError) as e:
            failed += 1
            print(f"FAIL {path}\n{e}\n")
        else:
            print(f"ok   {path}: {len(golden.covenants)} covenants")
    print(f"{len(files) - failed}/{len(files)} golden files valid")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
