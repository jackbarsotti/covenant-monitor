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
