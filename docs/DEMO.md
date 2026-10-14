# Two-minute demo

A script for recording. Total about 2:00. Cost about $1 in API usage.

## Before recording

- `.env` has `ANTHROPIC_API_KEY` and `SEC_USER_AGENT`.
- Start Phoenix in a separate terminal: `uvx --from arize-phoenix phoenix serve`.
- Open http://localhost:6006 in a browser tab.
- Do one throwaway run first so the EDGAR cache is warm and the timing on camera is shorter.
- Terminal font large enough to read; clear the screen.

## Shot list

| Time | Show | Say |
|---|---|---|
| 0:00–0:15 | README top section | "Covenant monitoring: give it a ticker, it reads the credit agreement and SEC financials, and flags how close the company is to breaching its loan covenants." |
| 0:15–0:30 | Mermaid diagram in the README | "Models read and judge. Code does the math, checks every quote, and decides when a human has to look." |
| 0:30–0:55 | Run `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006 uv run python -m covenant_monitor.agent.run start CACI` | "CACI has a leverage covenant with about ten percent headroom, so this run should stop for an analyst." |
| 0:55–1:15 | The `awaiting_approval` output: reasons, ratio 4.04x vs 4.50x, per-node cost table | "It paused: headroom is below the alert threshold. Every number here came from code, not the model. The run cost about a dollar." |
| 1:15–1:30 | Run `uv run python -m covenant_monitor.agent.run resume <thread_id> approve --note "checked with desk"` and scroll the memo | "I approve. The memo cites the agreement and the 10-K for every number." |
| 1:30–1:45 | Phoenix trace view: the run's span tree, one Opus review span with tokens | "One trace per run: every node, model call, and EDGAR tool call, with tokens and latency." |
| 1:45–2:00 | `evals/ERROR_ANALYSIS.md` score history, then the injection result in the README | "Evals went from 27 to 100 percent by fixing what the failures showed, and a planted prompt injection doesn't change the flags." |

## Fallback

If a live call is slow, cut to a pre-recorded run. The commands and outputs are the same.
