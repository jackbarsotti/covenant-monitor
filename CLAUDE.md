# covenant-monitor

Agent that reads a public company's credit agreement and SEC financials, checks its
financial covenants, and drafts a cited monitoring memo. Anything near a breach goes to
a human for approval first.

## Stack
- Python 3.12, managed with uv (`uv run`, `uv add`). Never use pip directly.
- LangGraph for orchestration. Raw Anthropic SDK inside nodes, no LangChain wrappers.
- Our own MCP server for SEC EDGAR access.
- pytest for tests, ruff for lint and format.

## Rules
- All math happens in code. Models extract terms and make judgment calls, never compute ratios.
- Every SEC request goes through the shared client: User-Agent from `.env`, under 10 requests per second.
- Never read, print, or commit `.env`.
- Tests must not hit the network. Mock HTTP with respx.
- Explain a change before making it. Keep diffs small.
- Record design choices in DECISIONS.md.
- Golden labels in `data/golden/` come from source documents only. One agent drafts them; a
  separate agent with fresh context verifies every value and never edits. Each file records
  its provenance. Never change a verified value without re-verifying it.

## Workflow
- Build in the order in `docs/ROADMAP.md`, one step at a time.
- Work on a branch. When a step's "Done when" checks pass, commit, push, open a PR with the
  diff summary in the body, and turn on auto-merge. GitHub merges it when CI is green.
- Write all PR text, commit messages, and messages to the user in tactical style: bottom line
  first, short sentences, bullets.
- Commit messages: short imperative subject line, blank line, bullets on what changed and why.
  No "WIP", "scaffold", or "step N" wording.
- Push only after CI-equivalent checks pass locally: ruff check, ruff format --check, pytest.

## Commands
- `uv run pytest -q`
- `uv run ruff check . && uv run ruff format --check .`
- `uv run python -m covenant_monitor.edgar BSX`
