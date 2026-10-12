"""LLM judge for memo faithfulness, calibrated before its verdicts count.

The calibration set (evals/judge_calibration.yaml) holds memos that are faithful or
unfaithful by construction. Each eval run measures the judge's agreement with them first;
memo verdicts only count toward the score if agreement meets the bar.
"""

import json
from pathlib import Path

import yaml
from pydantic import BaseModel

from covenant_monitor.agent import prompts
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.llm import LLM, Meter

CALIBRATION_FILE = Path("evals/judge_calibration.yaml")
TRUST_THRESHOLD = 0.9


class JudgeVerdict(BaseModel):
    passed: bool
    unsupported_claims: list[str]


class CalibrationCase(BaseModel):
    id: str
    expected: bool
    why: str
    facts: dict[str, str]
    memo: str


async def judge_memo(
    llm: LLM, meter: Meter, config: AgentConfig, memo: str, facts: dict[str, str]
) -> JudgeVerdict:
    prompt = f"Facts table:\n{json.dumps(facts, indent=1)}\n\nMemo:\n{memo}"
    return await llm.structured(
        meter=meter,
        model=config.models.judge,
        system=prompts.JUDGE,
        prompt=prompt,
        output=JudgeVerdict,
        effort="high",
    )


def load_cases(path: Path = CALIBRATION_FILE) -> list[CalibrationCase]:
    return [CalibrationCase(**c) for c in yaml.safe_load(path.read_text())["cases"]]


async def calibrate(llm: LLM, meter: Meter, config: AgentConfig, cases: list[CalibrationCase]):
    results = []
    for case in cases:
        verdict = await judge_memo(llm, meter, config, case.memo, case.facts)
        results.append(
            {
                "id": case.id,
                "expected": case.expected,
                "judged": verdict.passed,
                "agree": verdict.passed == case.expected,
                "unsupported_claims": verdict.unsupported_claims,
            }
        )
    agreement = sum(r["agree"] for r in results) / len(results) if results else 0.0
    return {
        "agreement": agreement,
        "trusted": agreement >= TRUST_THRESHOLD,
        "threshold": TRUST_THRESHOLD,
        "cases": results,
    }
