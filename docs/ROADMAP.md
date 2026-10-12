# Roadmap

Build order for covenant-monitor. Each step ends with tests passing, CI green, and one commit.

## 1. EDGAR client (done)

Ticker to CIK lookup, XBRL company facts, latest full-year value selection with restatement
handling, User-Agent enforcement. CI runs ruff and pytest.

## 2. MCP server for EDGAR

Expose EDGAR to the agent as a small set of read-only MCP tools, all going through one
shared client.

**Shared client** (refactor `edgar.py` into an async client)
- Rate limiter: token bucket, at most 9 requests per second across all callers. Inject the
  clock so tests never sleep.
- Retries on 429, 5xx, and timeouts. Exponential backoff with jitter, at most 4 attempts,
  honor `Retry-After`. No retry on other 4xx.
- On-disk cache under `data/cache/`, keyed by URL. Submissions and company facts expire after
  24 hours. Archived filing documents never change, so they never expire.
- Fail fast if `SEC_USER_AGENT` is missing or lacks a name and email.

**Server** (`src/covenant_monitor/mcp_server/server.py`, FastMCP over stdio, entry point
`covenant-monitor-mcp`)

| Tool | Input | Returns |
|---|---|---|
| `lookup_company` | ticker | CIK, name, exchange |
| `list_filings` | CIK, form types, since date | accession, form, filed date, primary document URL |
| `search_filings_text` | CIK, query, form types | matching documents with exhibit type (e.g. EX-10.1) and description, via EDGAR full-text search |
| `get_document_text` | sec.gov Archives URL, offset | plain text with HTML stripped, paged by character offset |
| `get_annual_financials` | CIK, us-gaap concepts | latest full-year value per concept, with period and source accession |

- Validate every input with pydantic: ticker format, numeric CIK, ISO dates, concept names.
- `get_document_text` accepts only `https://www.sec.gov/Archives/edgar/data/...` URLs, so the
  model can't make the server fetch arbitrary hosts.
- Errors come back as short messages the model can act on, never stack traces.
- Confirm each endpoint's response shape against a live call, then save trimmed responses as
  test fixtures.

**Tests** (no network, respx)
- The limiter never allows more than 9 calls in any one-second window under concurrency.
- 429 then 200 succeeds on retry and waits for `Retry-After`. 404 does not retry.
- A cache hit makes no second request.
- Non-SEC URLs and malformed tickers are rejected.
- Each tool returns the expected shape from fixtures.

**Done when** tests pass, `npx @modelcontextprotocol/inspector uv run covenant-monitor-mcp`
lists all five tools, and `lookup_company("BSX")` works live.

## 3. Golden dataset

Hand-labeled ground truth for three companies with credit agreements on EDGAR, starting with
the Deal Doc set.

- One file per company in `data/golden/`: agreement source (accession and exhibit), each
  financial covenant (type, threshold including step-downs, test frequency, section reference),
  the EBITDA add-backs allowed, the latest fiscal year's inputs, expected ratio, and headroom.
- Five questions per company with expected answers, two of them traps whose answer is not in
  the agreement.
- Labels are written by hand from the source documents. Code here is limited to the empty
  template, a schema, and a loader that validates the files.

## 4. Agent graph

LangGraph workflow with typed state, raw Anthropic SDK calls inside nodes.

1. `resolve_company` (code)
2. `fetch_agreement` and `fetch_financials` run in parallel through the MCP tools
3. `extract_covenants`: map over agreement sections, reduce into a typed covenant list where
   every field cites its section and quotes the source text
4. `compute_ratios` (code only, no model)
5. `review`: a different model with fresh context sees only the quoted excerpts and the
   extraction, and checks each field against the quote
6. Routing in code: review fails, go back to extraction (max 2 retries); headroom below the
   alert threshold, go to human approval; otherwise draft the memo
7. `human_approval`: LangGraph interrupt, resumes with approve, edit, or reject
8. `draft_memo`: every number traces to a filing or a ratio computation

- SQLite checkpointer so a run can pause and resume.
- Each node writes only its own state fields. Test it.
- Per-node budget caps on tokens and calls. Over budget, the run stops and says why.
- Prompt caching on agreement text.
- Models are config. Defaults: `claude-haiku-5-5` for the map step, `claude-sonnet-5-5` for
  extraction and memo, `claude-opus-5-5` for review.

## 5. Evals

- Runner executes the graph on the golden companies and scores it against the labels.
- Binary checks per field: threshold, frequency, add-back set, ratio within 0.01x, trap
  questions answered as not found, every quote present in the source text.
- LLM judge only for memo faithfulness. Measure its agreement with hand-labeled pass and fail
  memos before trusting it.
- Error analysis log in `evals/ERROR_ANALYSIS.md`: failure, category, fix, re-run result.
- CI runs unit tests on every push. Evals run on manual trigger with the API key as a GitHub
  secret, upload results as an artifact, and fail if the score drops below the saved baseline.

## 6. Observability

Tracing for every run (Langfuse or Arize Phoenix, choice logged in DECISIONS.md), plus a run
summary with cost, tokens, and latency per node.

## 7. Hardening and packaging

- Prompt injection test: plant an instruction in agreement text and assert the covenant flags
  don't change.
- Claude skill that runs the workflow.
- README case study: problem, architecture diagram, eval results, failure analysis, what I'd
  do next.
- Two-minute demo video.
