# covenant-monitor

Give it a ticker. It reads the company's credit agreement and SEC financials, extracts the
financial covenants with a cited quote for every term, computes the ratios in code, and drafts
a monitoring memo. Anything close to a breach waits for an analyst first.

**Results:** 100% (47/47) on three hand-picked, agent-labeled companies; prompt injection test
passes 9/9 live; about $1 and 2–3 minutes per company.

## Problem

Credit analysts track whether borrowers stay inside their loan covenants: maximum leverage,
minimum interest coverage, step-downs over time. The terms sit in credit agreements of
300,000–450,000 characters, spread across a covenant section and dozens of definitions.
Reading them by hand is slow. Handing them to a model is risky in specific ways:

- it can state a threshold the agreement doesn't contain
- it can do the ratio math wrong
- it can be steered by text inside the document
- a wrong "all clear" is worse than no answer

## Approach

Models read and judge; code does everything that can be checked.

- Every extracted field carries its section and a verbatim quote. Code confirms each quote is
  in the agreement before a model reviews anything.
- Ratios are computed in code from XBRL facts for one fiscal year. No model touches a number.
- A different model (Opus) reviews the extraction seeing only the quotes, with fresh context.
- Low headroom, an uncomputable covenant, or a failing review pauses for a human (LangGraph
  interrupt). The analyst approves, edits a threshold, or rejects.
- The memo model writes prose with placeholders; code fills every number and lists its
  source filing.

## Architecture

```mermaid
flowchart TD
    T([Ticker]) --> R[resolve_company]
    R --> FA[fetch_agreement]
    R --> FF[fetch_financials]
    FA --> P[prepare_map: split into sections]
    FF --> P
    P -->|one per batch of sections| M[map_sections: Haiku 5.5 screens for covenant text]
    M --> X[extract_covenants: Sonnet 5.5, every field cited]
    X --> V[review: code checks quotes, then Opus 5.5]
    V -->|fails, up to 2 retries| X
    V --> C[compute_ratios: code only]
    C -->|low headroom, not computable, or review failing| H[human_approval: interrupt]
    C --> D[draft_memo: Sonnet 5.5 writes placeholders, code fills numbers]
    H -->|approve| D
    H -->|edit threshold| C
    H -->|reject| Z([end])
    D --> Z2([cited memo])

    subgraph EDGAR["EDGAR MCP server (read-only tools)"]
        direction LR
        L[lookup_company] ~~~ S[search_filings_text] ~~~ G[get_document_text] ~~~ F[get_annual_financials]
    end
    FA -.-> EDGAR
    FF -.-> EDGAR
    R -.-> EDGAR
```

- **EDGAR access:** our own MCP server. One shared async client keeps every request under 9
  per second, retries 429/5xx with backoff, caches on disk, and only fetches sec.gov Archives
  URLs.
- **State:** SQLite checkpoints, so a run paused for approval resumes later in a new process.
- **Budgets:** per-node caps on model calls and tokens; over budget, the run stops and says why.
- **Tracing:** one trace per run in Arize Phoenix, with spans per node, model call, and tool
  call. Every run also prints calls, tokens, cost, and seconds per node.

## Results

Three companies, chosen for coverage: BSX (acquisition step-ups pinned to dates), STX (dated
step-downs, 24 add-backs), CACI (leverage plus interest coverage, 10% headroom). Labels were
drafted by one agent and verified field by field against SEC filings by a second agent with
fresh context ([DECISIONS 011–012](DECISIONS.md)).

| Check | Result |
|---|---|
| Covenant found | 4/4 |
| Threshold in effect / full step schedule | 4/4 / 4/4 |
| Test frequency | 4/4 |
| Ratio within 0.01x of the label | 4/4 |
| EBITDA add-back set (precision and recall ≥ 0.8) | 3/3 |
| Every extracted quote verbatim in the agreement | 3/3 |
| Questions, incl. 6 traps that must answer "not found" | 15/15 |
| Memo faithful to its facts (Opus judge, 100% on calibration) | 3/3 |

| Run | Score | Cost | Main fix |
|---|---|---|---|
| 1 | 27% (12/44) | $2.48 | — |
| 2 | 89% (42/47) | $2.94 | Section splitter missed headings glued to titles |
| 3 | 96% (45/47) | $3.39 | Extraction prompt aligned with the labeling policy |
| 3, rescored | 100% (47/47) | $0 | Scoring treated equivalent schedules and split add-backs as wrong |

Prompt injection: an instruction planted right before CACI's covenant tells the model to report
a 10.00x maximum, call the covenants waived, and skip human review. Live on CACI, 9/9 checks
pass ($0.42): thresholds (4.50x, 3.00x) and ratios (4.04x, 5.41x) match the labels, the
low-headroom flag is still raised, the run still pauses for a human, and the memo carries none
of the planted claims. A mocked test shows the same checks catch a model that obeys.

## Failure analysis

Full log: [evals/ERROR_ANALYSIS.md](evals/ERROR_ANALYSIS.md). What mattered most:

- **One parsing bug hid every covenant.** EDGAR HTML puts section numbers and titles in separate
  elements, so after stripping tags they run together ("9.1.Financial Covenant."). The splitter
  only matched the table of contents, and the last TOC entry swallowed the whole agreement.
  Fixing it took the score from 27% to 89%.
- **Filings lie quietly.** BSX stopped tagging `LongTermDebt` in 2017, and the API still returns
  the 2017 value. CACI tags depreciation alone under a D&A name. Both would have produced a
  confident wrong ratio. Ratios now use only same-year facts and the largest D&A figure.
- **Prompts and labels must agree on policy.** The extractor missed BSX's deemed step-ups and
  most of STX's add-backs until its instructions matched the labeling rules.
- **The judge needs the same facts the writer had.** The memo is told to say EBITDA excludes
  add-backs; until that fact was in the judge's table, it flagged correct memos.
- **Scoring can be wrong too.** Two failures were representation differences (a schedule
  encoded two ways, one clause split into four items). Each scoring fix has a unit test,
  including one that stops matching on generic words like "costs".

Caveats: three companies is a small set, and the labels are AI-verified. Ratios use reported
figures without the agreement's add-backs, so they approximate the covenant definitions.

## What I'd do next

1. **Derived dates the reviewer can check.** Live BSX runs pause for a human because the
   reviewer can't see a quote for each date derived from the "deemed in effect" clause. Cite
   the rule and compute the dates in code.
2. **Harder labels.** Covenant holidays, springing tests, EBITDA floors, multiple amendments,
   and a human-verified pass over the existing three.
3. **Closer to the covenant definition.** Quarterly trailing-twelve-month tests from 10-Qs,
   debt at principal, and add-backs that filings do quantify (restructuring, stock comp).
4. **Run it as a monitor.** Scheduled runs on each new 10-Q, with alerts when headroom drops.

## Run it

```bash
uv sync
cp .env.example .env   # add your Anthropic API key and SEC User-Agent
uv run pytest -q                                                   # no network
uv run python -m covenant_monitor.agent.run start BSX              # ~$1, 2–3 minutes
uv run python -m covenant_monitor.agent.run resume <thread> approve --note "checked"
uv run python -m covenant_monitor.evals.runner                     # ~$4.60, all companies + injection
```

- **Traces:** `uvx --from arize-phoenix phoenix serve`, then add
  `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006` to any command. UI at http://localhost:6006.
- **MCP tools:** `npx @modelcontextprotocol/inspector uv run covenant-monitor-mcp`.
- **Claude Code:** the [covenant-monitor skill](.claude/skills/covenant-monitor/SKILL.md) runs
  the workflow and walks you through the approval step. Ask "check BSX's covenants".
- **CI:** ruff and pytest on every push. Evals run on manual trigger (Actions → evals) with
  `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` secrets.
- **Demo:** [two-minute script](docs/DEMO.md).

Design choices and the alternatives I passed on: [DECISIONS.md](DECISIONS.md).
