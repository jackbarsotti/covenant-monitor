"""Run configuration: models per node, per-node budgets, and the alert threshold."""

from pydantic import BaseModel, Field


class Budget(BaseModel):
    """Caps for one node across the whole run, retries included."""

    max_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)


class Models(BaseModel):
    map_sections: str = "claude-haiku-5-5"
    extract_covenants: str = "claude-sonnet-5-5"
    review: str = "claude-opus-5-5"
    draft_memo: str = "claude-sonnet-5-5"
    answer_questions: str = "claude-sonnet-5-5"
    judge: str = "claude-opus-5-5"


def _default_budgets() -> dict[str, Budget]:
    return {
        "map_sections": Budget(max_calls=40, max_tokens=1_500_000),
        "extract_covenants": Budget(max_calls=3, max_tokens=600_000),
        "review": Budget(max_calls=3, max_tokens=150_000),
        "draft_memo": Budget(max_calls=2, max_tokens=80_000),
        # Q&A reads the whole agreement (cached after the first question).
        "answer_questions": Budget(max_calls=10, max_tokens=1_500_000),
        "judge": Budget(max_calls=20, max_tokens=300_000),
    }


class AgentConfig(BaseModel):
    models: Models = Models()
    budgets: dict[str, Budget] = Field(default_factory=_default_budgets)
    # Headroom below this share of the threshold goes to a human before the memo.
    alert_headroom_pct: float = Field(0.15, ge=0)
    max_review_retries: int = Field(2, ge=0)
    map_chunk_chars: int = Field(25_000, gt=1000)
    max_document_pages: int = Field(80, gt=0)
