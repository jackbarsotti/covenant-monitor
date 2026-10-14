"""Test doubles for the agent: a scripted LLM and a made-up company on mocked EDGAR."""

import json
from collections import defaultdict

from covenant_monitor.agent.llm import Meter
from covenant_monitor.agent.models import (
    CitedComponent,
    CitedFrequency,
    CitedThreshold,
    CitedType,
    ExtractedAddBack,
    ExtractedCovenant,
    Extraction,
    FieldVerdict,
    MemoDraft,
    ReviewVerdicts,
    SectionRelevance,
    UsageRecord,
)
from covenant_monitor.golden import GoldenCompany

CIK = 885725  # the ticker fixture maps BSX to this CIK; everything else here is made up
FACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json"
AGREEMENT_URL = f"https://www.sec.gov/Archives/edgar/data/{CIK}/000000000126000001/ex101.htm"

AGREEMENT_HTML = """
<p>CREDIT AGREEMENT among EXAMPLE CORP and the Lenders</p>
<p>TABLE OF CONTENTS</p>
<p>1.1. Defined Terms 1</p>
<p>7.1. Financial Covenant 40</p>
<p>SECTION 1 DEFINITIONS</p>
<p>1.1. Defined Terms.</p>
<p>&ldquo;Consolidated EBITDA&rdquo; means net income plus interest, taxes, depreciation and
amortization, plus restructuring charges not to exceed 10% of Consolidated EBITDA.</p>
<p>&ldquo;Leverage Ratio&rdquo; means the ratio of Consolidated Total Debt to Consolidated
EBITDA.</p>
<p>7.1. Financial Covenant.</p>
<p>The Borrower shall not permit the Leverage Ratio as of the last day of any fiscal quarter
to exceed 3.75 to 1.00.</p>
<p>7.2. Liens.</p>
<p>The Borrower shall not create any Lien except Permitted Liens.</p>
"""


def companyfacts(op_income=800, da=200, interest=100, debt=3000, cash=500):
    def duration(v):
        return {
            "units": {
                "USD": [
                    {
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "val": v,
                        "accn": "0000000001-26-000010",
                        "fy": 2025,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-02-20",
                    }
                ]
            }
        }

    def instant(v):
        return {
            "units": {
                "USD": [
                    {
                        "end": "2025-12-31",
                        "val": v,
                        "accn": "0000000001-26-000010",
                        "fy": 2025,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2026-02-20",
                    }
                ]
            }
        }

    return {
        "cik": CIK,
        "entityName": "EXAMPLE CORP",
        "facts": {
            "us-gaap": {
                "OperatingIncomeLoss": duration(op_income),
                "DepreciationDepletionAndAmortization": duration(da),
                "InterestExpense": duration(interest),
                "LongTermDebt": instant(debt),
                "CashAndCashEquivalentsAtCarryingValue": instant(cash),
            }
        },
    }


def good_extraction(threshold=3.75) -> Extraction:
    quote = f"to exceed {threshold:.2f} to 1.00"
    return Extraction(
        covenants=[
            ExtractedCovenant(
                name="Leverage Ratio",
                type=CitedType(
                    value="max_leverage", section="7.1", quote="shall not permit the Leverage Ratio"
                ),
                thresholds=[
                    CitedThreshold(ratio=threshold, effective_from=None, section="7.1", quote=quote)
                ],
                test_frequency=CitedFrequency(
                    value="quarterly",
                    section="7.1",
                    quote="as of the last day of any fiscal quarter",
                ),
                numerator=CitedComponent(
                    value="total_debt", section="1.1", quote="the ratio of Consolidated Total Debt"
                ),
                denominator=CitedComponent(
                    value="ebitda", section="1.1", quote="to Consolidated EBITDA"
                ),
            )
        ],
        ebitda_addbacks=[
            ExtractedAddBack(
                name="restructuring charges",
                cap="10% of Consolidated EBITDA",
                section="1.1",
                quote="restructuring charges not to exceed 10% of Consolidated EBITDA",
            ),
        ],
    )


def bad_extraction() -> Extraction:
    e = good_extraction()
    e.covenants[0].thresholds[0].quote = "to exceed 4.50 to 1.00"  # not in the text
    return e


def approve_all(prompt, document, n):
    fields = json.loads(prompt)
    return ReviewVerdicts(
        verdicts=[FieldVerdict(field_id=f["id"], ok=True, issue=None) for f in fields]
    )


def map_all(prompt, document, n):
    ids = [line.split("'")[1] for line in prompt.splitlines() if line.startswith("<section id=")]
    return SectionRelevance(relevant_section_ids=[i for i in ids if i in ("1.1", "7.1")])


GOOD_MEMO = MemoDraft(
    title="Covenant review: {company}",
    paragraphs=[
        "Reviewed {agreement_exhibit} filed under accession {agreement_accession}.",
        "{c0.name} was {c0.ratio} against a maximum of {c0.threshold} ({c0.section}), "
        "leaving headroom of {c0.headroom}.",
    ],
)


class FakeLLM:
    """Scripted answers per node. Goes through the real meter, so budgets apply."""

    def __init__(self, **handlers):
        self.handlers = {
            "map_sections": map_all,
            "extract_covenants": lambda p, d, n: good_extraction(),
            "review": approve_all,
            "draft_memo": lambda p, d, n: GOOD_MEMO,
            **handlers,
        }
        self.calls: dict[str, list[dict]] = defaultdict(list)

    async def structured(
        self, *, meter: Meter, model, system, prompt, output, document=None, effort=None
    ):
        meter.before_call()
        node = meter.node
        self.calls[node].append({"prompt": prompt, "document": document, "model": model})
        result = self.handlers[node](prompt, document, len(self.calls[node]))
        meter.after_call(UsageRecord(node=node, model=model, input_tokens=1000, output_tokens=100))
        assert isinstance(result, output)
        return result


# Golden labels for the made-up company above (scoring tests only).
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
