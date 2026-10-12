"""Answer analyst questions from the agreement text, or say the answer is not in it.

Used by the evals to score trap questions. The whole agreement is passed as one cached
document, so the questions after the first read it from cache.
"""

from pydantic import BaseModel

from covenant_monitor.agent import prompts
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.llm import LLM, Meter
from covenant_monitor.agent.models import Section
from covenant_monitor.agent.sections import family_text, normalize, render_sections


class QAAnswer(BaseModel):
    found: bool
    answer: str | None
    section: str | None
    quote: str | None


class CheckedAnswer(QAAnswer):
    question: str
    quote_verbatim: bool


async def answer_questions(
    llm: LLM, meter: Meter, config: AgentConfig, sections: list[Section], questions: list[str]
) -> list[CheckedAnswer]:
    document = render_sections(sections)
    answers = []
    for question in questions:
        a = await llm.structured(
            meter=meter,
            model=config.models.answer_questions,
            system=prompts.ANSWER,
            document=document,
            prompt=f"Question: {question}",
            output=QAAnswer,
            effort="medium",
        )
        verbatim = bool(
            a.found and a.quote and normalize(a.quote) in family_text(sections, a.section)
        )
        answers.append(CheckedAnswer(**a.model_dump(), question=question, quote_verbatim=verbatim))
    return answers
