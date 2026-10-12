"""Run the agent on every golden company and score it.

    uv run python -m covenant_monitor.evals.runner                      # all companies
    uv run python -m covenant_monitor.evals.runner --tickers BSX
    uv run python -m covenant_monitor.evals.runner --update-baseline
    uv run python -m covenant_monitor.evals.runner --rescore evals/results/run3.json

--rescore re-scores a saved run with the current scoring code, reading the run's state from
the eval checkpoint database. No model calls; use it after changing scoring only.

Each run: calibrate the memo judge, then for each company run the graph on the golden
agreement (auto-approving a human pause so a memo is drafted), answer the five golden
questions, judge the memo, and score. Exits non-zero if the score falls below the baseline.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from langgraph.types import Command

from covenant_monitor.agent import tracing
from covenant_monitor.agent.config import AgentConfig
from covenant_monitor.agent.graph import build_graph
from covenant_monitor.agent.llm import AgentStop, Ledger
from covenant_monitor.agent.nodes import Deps
from covenant_monitor.agent.pricing import cost_usd
from covenant_monitor.agent.qa import CheckedAnswer, answer_questions
from covenant_monitor.agent.run import checkpointer, execute, run_summary, session
from covenant_monitor.evals import judge
from covenant_monitor.evals.scoring import category, score_company
from covenant_monitor.golden import GOLDEN_DIR, GoldenCompany, load_all
from covenant_monitor.sec_client import user_agent

RESULTS_DIR = Path("evals/results")
# Model output varies between runs; allow about one flipped check before calling it a drop.
BASELINE_TOLERANCE = 0.025
BASELINE = Path("evals/baseline.json")
EVAL_DB = Path("data/eval_runs.sqlite")


async def evaluate_company(golden: GoldenCompany, graph, deps: Deps, judge_trusted: bool) -> dict:
    thread_id = f"eval-{golden.company.ticker}-{uuid.uuid4().hex[:8]}"
    graph_input = {"ticker": golden.company.ticker, "agreement_url": golden.agreement.url}
    summary = await execute(graph, deps, thread_id, graph_input)
    approval_request = summary["approval_request"]
    if summary["status"] == "awaiting_approval":
        summary = await execute(
            graph,
            deps,
            thread_id,
            Command(resume={"decision": "approve", "note": "auto-approved by the eval runner"}),
        )

    state = (await graph.aget_state({"configurable": {"thread_id": thread_id}})).values
    usage = list(state.get("usage", []))

    answers: list[CheckedAnswer] = []
    questions = [q.question for q in golden.questions]
    qa_error = None
    if state.get("sections"):
        meter = deps.ledger.meter("answer_questions")
        try:
            answers = await answer_questions(
                deps.llm, meter, deps.config, state["sections"], questions
            )
        except AgentStop as e:
            qa_error = str(e)
        usage += meter.records
    if len(answers) < len(questions):  # missing answers count as failures
        answers += [
            CheckedAnswer(
                question=q, found=True, answer=None, section=None, quote=None, quote_verbatim=False
            )
            for q in questions[len(answers) :]
        ]

    memo_verdict = None
    if state.get("memo"):
        meter = deps.ledger.meter("judge")
        try:
            memo_verdict = await judge.judge_memo(
                deps.llm, meter, deps.config, state["memo"].markdown, state["memo"].facts
            )
        except AgentStop:
            memo_verdict = None
        usage += meter.records

    checks, addbacks = score_company(
        golden,
        summary["status"],
        state.get("extraction"),
        state.get("ratios", []),
        state["agreement"].text if state.get("agreement") else "",
        answers,
        memo_verdict.passed if memo_verdict and judge_trusted else None,
    )
    return {
        "thread_id": thread_id,
        "status": summary["status"],
        "stopped": summary["stopped"],
        "approval_reasons": approval_request["reasons"] if approval_request else [],
        "checks": [c.model_dump() for c in checks],
        "passed": sum(c.passed for c in checks),
        "total": len(checks),
        "addbacks": addbacks,
        "answers": [a.model_dump() for a in answers],
        "qa_error": qa_error,
        "memo_verdict": memo_verdict.model_dump() if memo_verdict else None,
        "memo": state["memo"].markdown if state.get("memo") else None,
        "extraction": state["extraction"].model_dump() if state.get("extraction") else None,
        "ratios": [r.model_dump() for r in state.get("ratios", [])],
        "review": state["review"].model_dump() if state.get("review") else None,
        "relevant_section_ids": state.get("relevant_section_ids", []),
        "usage": [u.model_dump() for u in usage],
        "summary": run_summary(usage, state.get("timings", [])),
        "cost_usd": cost_usd(usage),
    }


async def run_evals(
    config: AgentConfig,
    goldens: dict[str, GoldenCompany],
    db_path: Path = EVAL_DB,
    llm=None,
    tools=None,
    calibration: list[judge.CalibrationCase] | None = None,
) -> dict:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    async with session(config, db_path, llm, tools) as (graph, deps):
        cal_meter = Ledger(config).meter("judge")
        cal = await judge.calibrate(
            deps.llm,
            cal_meter,
            config,
            calibration if calibration is not None else judge.load_cases(),
        )
        cal["cost_usd"] = cost_usd(cal_meter.records)

        companies = {}
        for ticker, golden in goldens.items():
            company_deps = Deps(deps.llm, deps.tools, config, Ledger(config))
            with tracing.span(f"eval {ticker}", "CHAIN", eval__ticker=ticker) as span:
                companies[ticker] = await evaluate_company(
                    golden, graph, company_deps, cal["trusted"]
                )
                result = companies[ticker]
                tracing.set_attributes(span, output__value=f"{result['passed']}/{result['total']}")

    return {
        "started_at": started,
        "models": config.models.model_dump(),
        "judge": cal,
        "companies": companies,
        **summarize(companies),
        "cost_usd": round(cal["cost_usd"] + sum(r["cost_usd"] for r in companies.values()), 4),
    }


def summarize(companies: dict) -> dict:
    checks = [c for r in companies.values() for c in r["checks"]]
    by_category: dict[str, list[bool]] = defaultdict(list)
    for c in checks:
        by_category[category(c["name"])].append(c["passed"])
    passed = sum(c["passed"] for c in checks)
    return {
        "passed": passed,
        "total": len(checks),
        "score": round(passed / len(checks), 4) if checks else 0.0,
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_category.items())},
    }


async def rescore(results: dict, db_path: Path = EVAL_DB) -> dict:
    goldens = load_all(GOLDEN_DIR)
    async with checkpointer(db_path) as saver:
        graph = build_graph(AgentConfig(), checkpointer=saver)
        for ticker, r in results["companies"].items():
            cfg = {"configurable": {"thread_id": r["thread_id"]}}
            state = (await graph.aget_state(cfg)).values
            verdict = r["memo_verdict"]
            checks, addbacks = score_company(
                goldens[ticker],
                r["status"],
                state.get("extraction"),
                state.get("ratios", []),
                state["agreement"].text if state.get("agreement") else "",
                [CheckedAnswer(**a) for a in r["answers"]],
                verdict["passed"] if verdict and results["judge"]["trusted"] else None,
            )
            r["checks"] = [c.model_dump() for c in checks]
            r["passed"], r["total"] = sum(c.passed for c in checks), len(checks)
            r["addbacks"] = addbacks
    results.update(summarize(results["companies"]))
    results["rescored_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return results


def print_summary(results: dict) -> None:
    j = results["judge"]
    print(f"judge agreement {j['agreement']:.0%} ({'trusted' if j['trusted'] else 'NOT trusted'})")
    for ticker, r in results["companies"].items():
        seconds = r.get("summary", {}).get("totals", {}).get("seconds", 0)
        print(
            f"\n{ticker}: {r['passed']}/{r['total']}  status={r['status']}  "
            f"${r['cost_usd']:.2f}  {seconds:.0f}s in nodes"
        )
        for c in r["checks"]:
            if not c["passed"]:
                print(f"  FAIL {c['name']}: {c['detail']}")
    print(f"\nby category: {results['by_category']}")
    print(
        f"score {results['score']:.2%} ({results['passed']}/{results['total']}), "
        f"cost ${results['cost_usd']:.2f}"
    )


def compare_to_baseline(results: dict, path: Path = BASELINE) -> bool:
    if not path.exists():
        print("no baseline yet")
        return True
    baseline = json.loads(path.read_text())
    ok = results["score"] >= baseline["score"] - baseline.get("tolerance", 0.0)
    print(f"baseline {baseline['score']:.2%} -> {results['score']:.2%}: {'OK' if ok else 'DROP'}")
    return ok


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="covenant_monitor.evals.runner")
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--rescore", type=Path, help="re-score a saved results file")
    args = parser.parse_args(argv)

    if args.rescore:
        results = await rescore(json.loads(args.rescore.read_text()))
        out = args.out or args.rescore.with_name(args.rescore.stem + "-rescored.json")
    else:
        load_dotenv()
        try:
            user_agent()  # fail here with one clear line, not inside the MCP subprocess
        except RuntimeError as e:
            print(f"error: {e}")
            return 2
        goldens = load_all(GOLDEN_DIR)
        if args.tickers:
            goldens = {t: goldens[t] for t in (x.upper() for x in args.tickers)}
        provider = tracing.setup()
        try:
            results = await run_evals(AgentConfig(), goldens)
        finally:
            if provider:
                provider.shutdown()
        out = args.out or RESULTS_DIR / f"{results['started_at'].replace(':', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str))
    print_summary(results)
    print(f"results: {out}")

    if args.update_baseline:
        BASELINE.write_text(
            json.dumps(
                {
                    "score": results["score"],
                    "passed": results["passed"],
                    "total": results["total"],
                    "tolerance": BASELINE_TOLERANCE,
                    "by_category": results["by_category"],
                    "from": out.name,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"baseline updated: {BASELINE}")
        return 0
    return 0 if compare_to_baseline(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
