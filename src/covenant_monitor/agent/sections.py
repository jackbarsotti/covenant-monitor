"""Split a credit agreement into numbered sections and batch them for the map step.

Headings look like "7.1. Financial Condition Covenants." or "Section 7.11 Leverage Ratio".
The table of contents repeats every heading, so for each section number we keep the
longest body. The definitions section is huge and holds the terms that matter most
(Consolidated EBITDA, Leverage Ratio), so long sections are split at each defined term.
"""

import re

from covenant_monitor.agent.models import Section

HEADING = re.compile(
    r"^(?:SECTION|Section)?\s*(\d{1,2}\.\d{1,2}[A-Za-z]?)\.?\s+([A-Z][^\n]{0,150})$",
    re.MULTILINE,
)
DEFINED_TERM = re.compile(r"^[“\"]([^”\"\n]{2,100})[”\"]\s+(?:shall\s+)?(?:means|has)", re.M)
SPLIT_LONG_OVER = 20_000


def split_sections(text: str) -> list[Section]:
    matches = list(HEADING.finditer(text))
    best: dict[str, Section] = {}
    order: list[str] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sid, heading = m.group(1), m.group(2).strip()
        body = text[m.start() : end].strip()
        if sid not in best:
            order.append(sid)
        if sid not in best or len(body) > len(best[sid].text):
            best[sid] = Section(id=sid, heading=heading, text=body)

    sections: list[Section] = []
    for sid in order:
        sec = best[sid]
        sections.extend(_split_definitions(sec) if len(sec.text) > SPLIT_LONG_OVER else [sec])
    return sections


def _split_definitions(sec: Section) -> list[Section]:
    starts = list(DEFINED_TERM.finditer(sec.text))
    if len(starts) < 2:
        return [sec]
    parts = [Section(id=sec.id, heading=sec.heading, text=sec.text[: starts[0].start()].strip())]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(sec.text)
        term = m.group(1).strip()
        parts.append(
            Section(id=f"{sec.id} “{term}”", heading=term, text=sec.text[m.start() : end].strip())
        )
    return [p for p in parts if p.text]


def chunk_sections(sections: list[Section], max_chars: int) -> list[list[Section]]:
    """Group consecutive sections into batches of at most max_chars (one oversized
    section becomes its own batch, truncated only in what the map step sees)."""
    chunks: list[list[Section]] = []
    current: list[Section] = []
    size = 0
    for sec in sections:
        if current and size + len(sec.text) > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(sec)
        size += len(sec.text)
    if current:
        chunks.append(current)
    return chunks


def render_sections(sections: list[Section], max_chars: int | None = None) -> str:
    out = []
    for sec in sections:
        body = sec.text if max_chars is None else sec.text[:max_chars]
        out.append(f"<section id={sec.id!r}>\n{body}\n</section>")
    return "\n\n".join(out)


def normalize(text: str) -> str:
    """Compare quotes without caring about curly quotes, dashes, or whitespace."""
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    text = text.replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", text).strip().lower()
