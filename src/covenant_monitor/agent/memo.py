"""Memo facts and rendering.

The model writes prose with {placeholders}, never numbers. Code fills every placeholder
from a facts table built from filings and ratio results, so each number in the memo
traces to its source. Any digit the model writes itself fails the check.
"""

import re

from covenant_monitor.agent.models import AgreementDoc, Company, Extraction, RatioResult

PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_.]+)\}")


def _money(v: float) -> str:
    return f"${v:,.0f}"


def build_facts(
    company: Company, agreement: AgreementDoc, extraction: Extraction, ratios: list[RatioResult]
) -> dict[str, str]:
    facts = {
        "company": company.name,
        "cik": str(company.cik),
        "agreement_accession": agreement.accession,
        "agreement_exhibit": agreement.exhibit_type,
        "agreement_filed": agreement.filed,
        "agreement_url": agreement.url,
        "ebitda_basis": (
            "EBITDA is reported operating income plus depreciation and amortization from "
            "XBRL filings; the agreement's add-backs are not applied, so ratios approximate "
            "the covenant definitions"
        ),
    }
    # Lets the memo say "everything was computed" (or not) with a fact behind it.
    facts["covenants_not_computed"] = (
        ", ".join(r.name for r in ratios if not r.computable) or "none"
    )
    for r in ratios:
        p = f"c{r.covenant_index}"
        cov = extraction.covenants[r.covenant_index]
        facts[f"{p}.name"] = r.name
        facts[f"{p}.section"] = cov.type.section
        if not r.computable:
            facts[f"{p}.reason"] = r.reason or "not computable"
            continue
        facts[f"{p}.ratio"] = f"{r.ratio:.2f}x"
        facts[f"{p}.threshold"] = f"{r.threshold:.2f}x"
        facts[f"{p}.headroom"] = f"{r.headroom:+.2f}x"
        facts[f"{p}.headroom_pct"] = f"{r.headroom_pct:+.1%}"
        facts[f"{p}.period_end"] = r.period_end
        facts[f"{p}.numerator"] = _money(r.numerator)
        facts[f"{p}.denominator"] = _money(r.denominator)
        for concept, fact in r.inputs.items():
            facts[f"{p}.{concept}"] = f"{_money(fact.value)} (accession {fact.accession})"
    return facts


def stray_digits(paragraphs: list[str], facts: dict[str, str]) -> list[str]:
    """Digits outside placeholders, ignoring digits that are part of the company name."""
    found = []
    for para in paragraphs:
        text = PLACEHOLDER.sub("", para).replace(facts.get("company", "\0"), "")
        found += re.findall(r"[^\s]*\d[^\s]*", text)
    return found


def unknown_placeholders(paragraphs: list[str], facts: dict[str, str]) -> list[str]:
    return sorted({k for p in paragraphs for k in PLACEHOLDER.findall(p) if k not in facts})


def render(title: str, paragraphs: list[str], facts: dict[str, str]) -> str:
    used: list[str] = []

    def fill(m: re.Match) -> str:
        key = m.group(1)
        if key not in used:
            used.append(key)
        return facts[key]

    body = "\n\n".join(PLACEHOLDER.sub(fill, p) for p in paragraphs)
    sources = "\n".join(f"- `{k}`: {facts[k]}" for k in used)
    return f"# {PLACEHOLDER.sub(fill, title)}\n\n{body}\n\n## Sources\n\n{sources}\n"
