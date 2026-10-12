# Decisions

Short records of design choices: what I picked, what I passed on, and why.

## 001: LangGraph for orchestration

The workflow has real branches. Agreement extraction and the financials pull can run in
parallel and join before the ratio math. A reviewer can send extraction back for a retry.
Anything near a covenant breach has to pause for a human, then resume where it stopped.
LangGraph gives me typed state, checkpoints, and interrupts for those pieces, so I don't
have to build them. Inside each node I call the Anthropic SDK directly, so every prompt
stays visible and testable.

Passed on: a single tool-calling loop. It can't guarantee the human approval step, and a
failure halfway through means starting over.

## 002: Build my own MCP server for EDGAR

Public EDGAR MCP servers already exist. I'm writing my own because the agent only needs a
handful of read-only tools, and fewer tools means less for the model to misuse. Owning the
server also means I own the rate limiting, caching, and input checks, and I can later
expose my Deal Doc retrieval as a tool, which no public server has.

## 003: uv for Python environments

One tool handles the Python version, dependencies, and a lockfile, and it's fast in CI.
`uv sync --locked` in CI fails if the lockfile drifts from pyproject.toml, so the build
that passes on GitHub is the build I ran locally.

## 004: Sliding-window rate limiter instead of a token bucket

SEC allows 10 requests per second and I cap at 9. A token bucket that allows any burst can
let more than 9 through in a one-second window: drain a full bucket, then the refill adds
more before the second is up. A bucket of size one avoids that, but then its float spacing
decides whether the tenth call lands just inside the window. I keep the times of the last 9
requests instead, and a new one waits until the oldest is a full second old. That holds the
cap by construction and still allows a burst of 9. The clock and sleep are injected, so the
concurrency test runs on a fake clock and never sleeps.

## 005: MCPServer from the mcp 2.x SDK

The roadmap says FastMCP, but mcp 2.x renamed FastMCP to `MCPServer` and changed some
APIs. I'm on 2.3, so the server uses `MCPServer` instead of pinning `mcp<2`. Anticipated
failures raise `ToolError` with a short message. Anything else reaches the model only as
"Error executing tool X", and the traceback stays in the server log.

## 006: Golden labels in YAML, checked by a strict schema

The golden files are written by hand, so the format has to be easy to type and allow
comments next to the numbers they explain. YAML does both; JSON does neither. Each file is
validated by a pydantic schema that rejects unknown keys, so a misspelled field fails
instead of being silently dropped. The loader checks shape only: counts, formats, date
order, and that every expected result names a covenant. It never recomputes the expected
ratios, because those are the labels the ratio code gets scored against.

## 007: Code checks before the reviewer model

Every extracted field cites a section and quotes it. Before the Opus reviewer sees
anything, code confirms each quote appears verbatim in the cited section (ignoring curly
quotes and whitespace), that dates parse, and that thresholds are positive. A failure there
goes straight back to extraction with the exact problems, and the reviewer is never called.
The reviewer only judges what code can't: whether a real quote supports the claimed value.
After two retries the run goes to a human rather than guessing.

## 008: The memo model never writes a number

The memo model writes prose with placeholders like `{c0.ratio}`, and code fills them from a
facts table built from XBRL filings and the ratio results. Any digit typed outside a
placeholder, or an unknown key, fails the draft (one retry, then the run stops). Every
number in the memo is listed under Sources with its accession.

## 009: Ratios from same-year XBRL, without add-backs

Ratio inputs come from the latest 10-K's XBRL. Companies change tags over time, and a tag
they stopped using still returns its last value, so only facts for the latest fiscal year
end are used; a missing input makes the covenant "not computable" and sends it to a human.
EBITDA is operating income plus D&A as reported. The agreement's add-backs are extracted but
not applied, since filings don't break them out, so the memo says the ratio approximates the
covenant definition.

## 010: Agreement choice and refusal fallbacks

Without an explicit `agreement_url`, the run takes the newest EX-10 credit agreement from
EDGAR full-text search, skipping amendments, and the lowest exhibit number when one filing
has several. Evals pass the golden agreement URL so scoring never depends on that guess.
Sonnet and Opus calls opt into server-side refusal fallbacks (`fallbacks: "default"`), so a
false-positive refusal reroutes instead of failing the run; Haiku has no fallback, and any
refusal that remains stops the run with a reason.

## 011: AI-drafted, AI-verified golden labels

Hand labeling is the gold standard but was blocking progress. Labels are now drafted by one
agent from the source documents and checked field by field by a second agent with fresh
context that can only report, never edit. Each file records `provenance`, so eval reports
can show which scores rest on AI-checked labels.

The risk is circularity: the system under test is also Claude, so shared blind spots can
pass both the labels and the agent. Mitigations: every label quotes the agreement verbatim
and cites the section; expected ratios are computed in code from cited 10-K figures, not by
the model; the verifier re-derives each value from the source rather than reading the
draft's reasoning; and trap questions are checked against the full agreement. Any label a
person later checks moves to `ai_drafted_human_verified`.

## 012: Labeling policies

Rules settled while verifying the first three companies. Future labels follow them.
- Thresholds: dated step-downs are listed with the first date they apply. Acquisition
  step-ups are listed only when the agreement deems them in effect on fixed dates (BSX);
  elective ones stay out of the schedule and go in a comment (CACI's Leverage Increase
  Period).
- Expected threshold is the level in effect on the fiscal year end.
- Each covenant quote states the threshold `expected` uses, verbatim. `section` is where the
  covenant is imposed; a comment names the definition section when the quote comes from one.
- Add-backs include the EBITDA definition's items and the Consolidated Net Income
  exclusions that raise EBITDA, named for their charge side. Income inclusions are left out.
  A cap holds an amount limit only; conditions go in the name.
- Ratio inputs are as-reported 10-K XBRL values for the same fiscal year end, debt at
  carrying amount, no agreement add-backs.

## 013: Eval design

- Binary checks only, except memo faithfulness. Each golden covenant is checked for:
  found, threshold in effect at fiscal year end, full step schedule, test frequency, and
  ratio within 0.01x. Also the add-back set, every extracted quote verbatim in the
  agreement, and five questions per company. Score = checks passed / checks run.
- Questions use a small Q&A function, not a graph node: the whole agreement is one cached
  document and the model must answer "not found" when the text doesn't say. A normal
  question passes when it cites the labeled section number with a verbatim quote; a trap
  passes when it says not found. Answer wording is not graded.
- Add-backs are compared as sets by word overlap between names: an extracted item counts if
  it matches any labeled add-back, and a label counts if any item matches it. Plurals are
  stemmed and generic words ("costs", "charges") ignored. Passes at precision and recall of
  0.8 or more. Splitting one clause into parts is not penalized; precision and recall are
  reported so error analysis can see near misses.
- Threshold schedules match when they give the same limit on every date either one changes,
  so two encodings of one schedule compare equal.
- The memo judge (Opus) is calibrated every run on memos built to be faithful or not, each
  unfaithful one with one planted unsupported claim. Its memo verdicts count only at 90%
  agreement or better.
- A human pause is auto-approved during evals so every run reaches a memo.
- CI runs evals only on manual trigger, with the API key and SEC User-Agent as repo
  secrets, uploads the results JSON, and fails if the score drops below evals/baseline.json
  by more than its tolerance (0.025, about one check), since model output varies run to run.
- `--rescore` re-scores a saved run from the eval checkpoint database with no model calls.
  Scoring-only fixes are verified that way instead of paying for a new run.

## 014: Arize Phoenix for tracing, with our own spans

Phoenix runs locally with one command and needs no account, so traces work on any machine
and in CI without new secrets. Langfuse would need either a cloud account and keys or a
self-hosted stack (Postgres, ClickHouse, Redis). Both accept OpenTelemetry, so switching
later means changing the exporter, not the spans.

Spans are written by our own wrappers, not auto-instrumentation: a root span per run, one
per graph node, one per model call, and one per MCP tool call, using OpenInference attribute
names so Phoenix shows models, tokens, inputs, and outputs. The instrumentation packages
patch SDK internals and may miss `beta.messages.parse` with fallbacks; the LangGraph one
traces through LangChain callbacks. Ours covers exactly the calls we make. Tracing is off
unless PHOENIX_COLLECTOR_ENDPOINT is set; tests use an in-memory exporter.

Every node also records its own timing in state, so the run summary (calls, tokens,
estimated cost, seconds per node) works with or without a tracing backend.
