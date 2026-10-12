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
