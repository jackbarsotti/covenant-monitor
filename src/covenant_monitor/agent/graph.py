"""The workflow graph. Routing is code; models only run inside nodes.

resolve_company
  -> fetch_agreement + fetch_financials (parallel)
  -> prepare_map -> map_sections (one per batch of sections) -> extract_covenants
  -> review -> (fail, retries left) extract_covenants
            -> compute_ratios -> (needs a human) human_approval -> draft_memo
                              -> draft_memo
"""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from covenant_monitor.agent import nodes
from covenant_monitor.agent.nodes import Deps, State, approval_reasons
from covenant_monitor.agent.sections import chunk_sections


def _stopped(state) -> bool:
    return bool(state.get("stopped"))


def after_resolve(state):
    return END if _stopped(state) else ["fetch_agreement", "fetch_financials"]


def after_prepare(state, config):
    if _stopped(state):
        return END
    chunks = chunk_sections(state["sections"], config.map_chunk_chars)
    return [Send("map_sections", {"sections": chunk}) for chunk in chunks]


def after_extract(state):
    return END if _stopped(state) else "review"


def after_review(state, config):
    if _stopped(state):
        return END
    retries_left = state["extraction_attempts"] <= config.max_review_retries
    if not state["review"].passed and retries_left:
        return "extract_covenants"
    return "compute_ratios"


def after_ratios(state, config):
    if _stopped(state):
        return END
    approval = state.get("approval")
    if approval and approval.decision == "edit":
        return "draft_memo"  # the analyst already signed off on the edited numbers
    return "human_approval" if approval_reasons(state, config) else "draft_memo"


def after_approval(state):
    approval = state["approval"]
    if approval.decision == "reject":
        return END
    return "compute_ratios" if approval.decision == "edit" else "draft_memo"


def build_graph(config, checkpointer=None):
    g = StateGraph(State, context_schema=Deps)
    for name in nodes.NODE_WRITES:
        g.add_node(name, getattr(nodes, name))

    g.add_edge(START, "resolve_company")
    g.add_conditional_edges("resolve_company", after_resolve)
    g.add_edge(["fetch_agreement", "fetch_financials"], "prepare_map")
    g.add_conditional_edges("prepare_map", lambda s: after_prepare(s, config))
    g.add_edge("map_sections", "extract_covenants")
    g.add_conditional_edges("extract_covenants", after_extract)
    g.add_conditional_edges("review", lambda s: after_review(s, config))
    g.add_conditional_edges("compute_ratios", lambda s: after_ratios(s, config))
    g.add_conditional_edges("human_approval", after_approval)
    g.add_edge("draft_memo", END)
    return g.compile(checkpointer=checkpointer)
