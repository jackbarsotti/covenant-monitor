---
name: covenant-monitor
description: Check a public company's credit agreement covenants. Runs the covenant-monitor workflow on a ticker (fetch the credit agreement and SEC financials, extract the financial covenants, compute ratios, review), walks the user through the human approval step, and shows the cited monitoring memo. Use when the user asks to check, monitor, or review a company's loan or credit agreement covenants, leverage covenant, or covenant headroom.
---

# Covenant monitor

Runs the workflow in this repo on one ticker and reports the result. Work from the repo root.

## Before running

- Each run calls the Anthropic API: about $0.30–1.10 per company. Tell the user the estimate
  and confirm before starting.
- The run needs `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` in `.env`. Never read, print, or
  edit `.env`. If the run exits with a `SEC_USER_AGENT` error, pass the message to the user.
- Optional tracing: if the user wants to see the trace, start Phoenix with
  `uvx --from arize-phoenix phoenix serve` (UI at http://localhost:6006) and prefix the run
  command with `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006`.

## Run

```bash
uv run python -m covenant_monitor.agent.run start <TICKER>
```

Add `--agreement-url <https://www.sec.gov/Archives/edgar/data/...>` when the user names a
specific agreement. Without it, the run takes the newest credit agreement exhibit on EDGAR.

The output is JSON with `thread_id` and `status`, then a per-node table (calls, tokens,
cost, seconds), then the memo if one was drafted. Act on `status`:

- `done`: report (see Report).
- `stopped`: give the user the `stopped` reason as written. Don't retry on your own.
- `awaiting_approval`: go to Approval.

## Approval

The run paused for a human because of the reasons in `approval_request.reasons`: low
headroom, a covenant that couldn't be computed, or a review that kept failing.

1. Show the user each reason, each ratio (name, ratio, threshold, headroom_pct), and the
   `review_issues`.
2. Ask the user to choose approve, edit, or reject. Never choose for them: this pause is the
   human check the workflow exists for.
3. Resume with their choice and their reason:

```bash
uv run python -m covenant_monitor.agent.run resume <thread_id> approve --note "<reason>"
uv run python -m covenant_monitor.agent.run resume <thread_id> edit --threshold <covenant index>=<value> --note "<reason>"
uv run python -m covenant_monitor.agent.run resume <thread_id> reject --note "<reason>"
```

`edit` recomputes the ratios with the user's threshold and drafts the memo. `reject` ends the
run with no memo.

## Report

- Bottom line first: each covenant's ratio against its threshold, the headroom, and whether a
  human approved or edited.
- Then the memo as printed. Every number in it traces to a filing or a ratio computation;
  don't restate numbers in your own words.
- Keep the memo's caveat: EBITDA is reported operating income plus D&A, without the
  agreement's add-backs, so ratios approximate the covenant definitions.
- End with cost and time from the `totals` row.
