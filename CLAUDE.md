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
- Explain a change before making it. Keep diffs small and commit after each passing check.
- Record design choices in DECISIONS.md.

## Commands
- `uv run pytest -q`
- `uv run ruff check . && uv run ruff format --check .`
- `uv run python -m covenant_monitor.edgar BSX`
