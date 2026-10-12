# covenant-monitor

Monitors the financial covenants in public companies' credit agreements using SEC EDGAR data.

Give it a ticker. It pulls the credit agreement and financials from EDGAR, extracts the
covenant terms, computes the ratios in code, and flags how much headroom is left. Anything
close to a breach waits for analyst approval before it drafts a cited monitoring memo.

## Status

| Component | Status |
|---|---|
| EDGAR client: ticker lookup, XBRL company facts, annual value selection | Done |
| MCP server exposing EDGAR as read-only tools | Done |
| Golden dataset of hand-labeled covenant terms | Schema done, labels in progress |
| LangGraph workflow with reviewer and human approval | Done (mocked tests; no live model run yet) |
| Evals in CI | Planned |

## Setup

```bash
uv sync
cp .env.example .env   # add your Anthropic API key and SEC User-Agent
uv run pytest -q
uv run python -m covenant_monitor.edgar BSX
npx @modelcontextprotocol/inspector uv run covenant-monitor-mcp   # browse the MCP tools
uv run python -m covenant_monitor.agent.run start BSX               # needs ANTHROPIC_API_KEY
```

```
BOSTON SCIENTIFIC CORPORATION (CIK 0000885725)
us-gaap concepts reported: 750
OperatingIncomeLoss, FY ending 2025-12-31: $3,613,000,000
```

## Design

Design choices and the alternatives I passed on are logged in [DECISIONS.md](DECISIONS.md).
