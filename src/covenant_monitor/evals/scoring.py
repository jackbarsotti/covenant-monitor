"""Binary checks: each one passes or fails, and the score is the share that pass.

Checks per company:
- run_completed: the run produced a memo
- covenant[<type>].found / .threshold_in_effect / .schedule / .frequency / .ratio
- addbacks: the add-back set matches (fuzzy name match, precision and recall >= 0.8)
- quotes_verbatim: every extracted quote appears in the agreement text
- question[i]: answered from the right section with a verbatim quote, or "not found" for a trap
- memo_faithful: the calibrated judge passes the memo (only counted if the judge is trusted)
"""

import re

from pydantic import BaseModel

from covenant_monitor.agent.models import Extraction, RatioResult
from covenant_monitor.agent.qa import CheckedAnswer
from covenant_monitor.agent.ratios import NotComputable, applicable_threshold
from covenant_monitor.agent.sections import normalize
from covenant_monitor.golden import GoldenCompany

RATIO_TOLERANCE = 0.01
ADDBACK_MATCH = 0.5
ADDBACK_PASS = 0.8
STOPWORDS = set(
    "a an and any are as at be by for from in into is it its of on or other such that the "
    "to under with consolidated net income ebitda excluded extent amount amounts".split()
)
# Words nearly every add-back name contains; matching on them alone would pair unrelated
# items ("Other charges" with "Restructuring charges").
GENERIC = {"charge", "cost", "expense", "loss", "fee", "item"}


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""


def section_number(text: str | None) -> str | None:
    """'Section 8.11(a)' and '8.11' both give '8.11'; leading zeros don't matter."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})\.(\d{1,2})", text)
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else None


def _stem(word: str) -> str:
    """Plural to singular, enough to match "losses"/"loss" and "facilities"/"facility"."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es") and word[:-2].endswith(("ss", "x", "ch", "sh")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    stems = {_stem(w) for w in words if w not in STOPWORDS and len(w) > 2}
    return stems - GENERIC


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def match_addbacks(golden: list[str], extracted: list[str]) -> dict:
    """Set comparison on word overlap. An extracted item counts if it matches any labeled
    add-back; a labeled add-back counts if any extracted item matches it. So an agent that
    lists one clause as several items (or several clauses as one) isn't penalized."""

    def hit(name: str, others: list[str]) -> bool:
        return any(_similarity(name, o) >= ADDBACK_MATCH for o in others)

    found = [g for g in golden if hit(g, extracted)]
    supported = [e for e in extracted if hit(e, golden)]
    return {
        "matched": len(found),
        "precision": len(supported) / len(extracted) if extracted else (1.0 if not golden else 0.0),
        "recall": len(found) / len(golden) if golden else 1.0,
        "missed": [g for g in golden if g not in found],
        "extra": [e for e in extracted if e not in supported],
    }


def level_at(steps: list[tuple[float, str | None]], day: str) -> float | None:
    """Threshold in effect on an ISO date under (ratio, effective_from) steps."""
    in_effect = [(d or "", r) for r, d in steps if d is None or d <= day]
    return max(in_effect)[1] if in_effect else None


def same_schedule(want: list[tuple[float, str | None]], have: list[tuple[float, str | None]]):
    """Schedules match if they give the same level on every date either one changes. Two
    encodings of one schedule (a dated first step vs a null one) compare equal."""
    days = sorted({d for _, d in want + have if d})
    if not days:
        return {r for r, _ in want} == {r for r, _ in have}
    return all(
        (a := level_at(want, d)) is not None
        and (b := level_at(have, d)) is not None
        and abs(a - b) < 1e-6
        for d in days
    )


def golden_threshold(steps, period_end) -> float:
    """Level in effect on period_end under a golden schedule (null start = from closing)."""
    in_effect = [s for s in steps if s.effective_from is None or s.effective_from <= period_end]
    return max(in_effect, key=lambda s: s.effective_from or period_end.min).ratio


def score_company(
    golden: GoldenCompany,
    status: str,
    extraction: Extraction | None,
    ratios: list[RatioResult],
    agreement_text: str,
    answers: list[CheckedAnswer],
    memo_passed: bool | None,
) -> tuple[list[Check], dict]:
    checks = [Check(name="run_completed", passed=status == "done", detail=status)]
    period_end = golden.latest_fiscal_year.period_end.isoformat()
    extracted = extraction.covenants if extraction else []
    expected = {e.covenant: e for e in golden.latest_fiscal_year.expected}

    for cov in golden.covenants:
        prefix = f"covenant[{cov.type.value}]"
        idx = next((i for i, c in enumerate(extracted) if c.type.value == cov.type), None)
        checks.append(Check(name=f"{prefix}.found", passed=idx is not None))
        if idx is None:
            for part in ("threshold_in_effect", "schedule", "frequency"):
                checks.append(Check(name=f"{prefix}.{part}", passed=False, detail="not found"))
            if cov.type in expected:
                checks.append(Check(name=f"{prefix}.ratio", passed=False, detail="not found"))
            continue
        got = extracted[idx]

        want_threshold = (
            expected[cov.type].threshold
            if cov.type in expected
            else golden_threshold(cov.thresholds, golden.latest_fiscal_year.period_end)
        )
        try:
            have_threshold = applicable_threshold(got, period_end)
        except (NotComputable, ValueError) as e:
            have_threshold, note = None, str(e)
        else:
            note = f"got {have_threshold}, want {want_threshold}"
        checks.append(
            Check(
                name=f"{prefix}.threshold_in_effect",
                passed=have_threshold is not None and abs(have_threshold - want_threshold) < 1e-6,
                detail=note,
            )
        )

        want_steps = [
            (round(t.ratio, 2), t.effective_from.isoformat() if t.effective_from else None)
            for t in cov.thresholds
        ]
        have_steps = [(round(t.ratio, 2), t.effective_from) for t in got.thresholds]
        checks.append(
            Check(
                name=f"{prefix}.schedule",
                passed=same_schedule(want_steps, have_steps),
                detail=f"got {sorted(have_steps, key=str)}, want {sorted(want_steps, key=str)}",
            )
        )
        checks.append(
            Check(
                name=f"{prefix}.frequency",
                passed=got.test_frequency.value == cov.test_frequency,
                detail=f"got {got.test_frequency.value}, want {cov.test_frequency.value}",
            )
        )
        if cov.type in expected:
            result = next((r for r in ratios if r.covenant_index == idx), None)
            want = expected[cov.type].ratio
            ok = bool(result and result.computable and abs(result.ratio - want) <= RATIO_TOLERANCE)
            have = result.ratio if result and result.computable else (result and result.reason)
            checks.append(
                Check(name=f"{prefix}.ratio", passed=ok, detail=f"got {have}, want {want}")
            )

    addbacks = match_addbacks(
        [a.name for a in golden.ebitda_addbacks],
        [a.name for a in extraction.ebitda_addbacks] if extraction else [],
    )
    checks.append(
        Check(
            name="addbacks",
            passed=addbacks["precision"] >= ADDBACK_PASS and addbacks["recall"] >= ADDBACK_PASS,
            detail=f"precision {addbacks['precision']:.2f}, recall {addbacks['recall']:.2f}",
        )
    )

    text = normalize(agreement_text)
    quotes = []
    if extraction:
        for c in extraction.covenants:
            quotes += [c.type.quote, c.test_frequency.quote, c.numerator.quote, c.denominator.quote]
            quotes += [t.quote for t in c.thresholds]
        quotes += [a.quote for a in extraction.ebitda_addbacks]
    missing = [q for q in quotes if normalize(q) not in text]
    checks.append(
        Check(
            name="quotes_verbatim",
            passed=bool(quotes) and not missing,
            detail=f"{len(quotes) - len(missing)}/{len(quotes)} found",
        )
    )

    for i, (q, a) in enumerate(zip(golden.questions, answers, strict=True)):
        if q.trap:
            ok, detail = (
                not a.found,
                "trap: " + ("said not found" if not a.found else a.answer or ""),
            )
        else:
            same_section = section_number(a.section) == section_number(q.section)
            ok = a.found and same_section and a.quote_verbatim
            detail = (
                f"found={a.found}, section {a.section!r} vs {q.section!r}, "
                f"quote_verbatim={a.quote_verbatim}"
            )
        checks.append(Check(name=f"question[{i}]", passed=ok, detail=detail))

    if memo_passed is not None:
        checks.append(Check(name="memo_faithful", passed=memo_passed))
    return checks, addbacks


def category(check_name: str) -> str:
    """covenant[max_leverage].ratio -> ratio; question[3] -> question."""
    name = re.sub(r"\[.*?\]", "", check_name)
    return name.split(".")[-1] if "." in name else name
