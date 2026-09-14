"""Phase 6 (stretch): expose the real, shipped retriever as MCP tools, so an
IDE agent (Claude Code, Cursor) can call `blast_radius` and `resolve_issue`
directly instead of this project building its own agent loop to compete with
one.

Matches CONTEXT.md's own thesis -- "the contribution is the retriever, not
the agent loop" -- by shipping exactly what is real. **Neither tool calls a
model or produces a patch.** `resolve_issue` returns the graph-grounded
`Context` (ranked files and snippets under a token budget) an agent needs to
do the fix itself; the tool's own description says so, so a calling agent
never mistakes this for a finished resolution. `agent/orchestrator.py` is
still a Phase-3 stub that emits `STUB_PATCH`; this server never touches it,
on purpose -- wiring a tool named `resolve_issue` to a placeholder patch
would be exactly the kind of stubbed-but-scoreable-looking result
CONTEXT.md's invariants exist to rule out.

Each tool is a thin wrapper around a plain function (`_resolve_issue`,
`_blast_radius_query`) so tests can call the real logic without a live MCP
session -- the same split `GraphRetriever.rank()`/`.retrieve()` and the CLI
already use. `mcp` itself (the SDK, ~20 packages: starlette, uvicorn, jsonrpc
plumbing) is imported lazily inside `build_server()`, not at module level --
same reason `embedding_retriever.py` imports `sentence_transformers` inside a
function rather than at the top: this module must stay importable, and its
plain functions testable, on the base CI install with no extras.

Run with:
    uv run --extra mcp python -m cartographer.mcp.server
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..graph.blast_radius import blast_radius as _run_blast_radius
from ..graph.graph_builder import build_graph
from ..retrieval import MODES, RepoRef, build_retriever
from ..retrieval.base import Issue
from ..retrieval.seeds import extract_seeds

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

INSTRUCTIONS = (
    "Graph-grounded code retrieval for issue resolution. Given a repo "
    "checkout and an issue, ranks the files and symbols most likely to need "
    "a fix using a real dependency graph (imports/calls/inherits edges, "
    "resolved with confidence, not embeddings). Neither tool calls a model "
    "or produces a patch -- use the returned context or ranking to do the "
    "actual fix yourself."
)


def _resolve_issue(
    repo: str,
    issue_text: str,
    *,
    mode: str = "graph",
    k: int = 12,
    budget_tokens: int = 8000,
) -> dict[str, Any]:
    """Plain function the `resolve_issue` tool wraps."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r} -- expected one of {sorted(MODES)}")
    retriever = build_retriever(mode, k=k)
    ctx = retriever.retrieve(
        Issue(id="mcp", title="", body=issue_text),
        RepoRef(root=Path(repo)),
        budget_tokens=budget_tokens,
    )
    return {
        "mode": ctx.mode,
        "token_estimate": ctx.token_estimate,
        "files": list(ctx.files),
        "snippets": [
            {
                "path": s.path,
                "start_line": s.start_line,
                "end_line": s.end_line,
                "text": s.text,
                "symbol": s.symbol,
                "reason": s.reason,
            }
            for s in ctx.snippets
        ],
        "stats": ctx.stats,
    }


def _blast_radius_query(
    repo: str,
    issue_text: str,
    *,
    hops: int = 3,
    min_confidence: float = 0.0,
    limit: int = 20,
) -> dict[str, Any]:
    """Plain function the `blast_radius` tool wraps."""
    cg = build_graph(Path(repo))
    seeds = extract_seeds(issue_text, cg)
    weights = {s.node: s.weight for s in seeds}
    br = _run_blast_radius(
        cg, list(weights), hops=hops, min_confidence=min_confidence, seed_weights=weights
    )
    return {
        "seeds": [
            {"node": s.node, "path": cg.g.nodes[s.node]["path"], "weight": s.weight,
             "evidence": s.evidence}
            for s in seeds
        ],
        "ranked": [
            {
                "node": r.node,
                "path": cg.g.nodes[r.node]["path"],
                "qualname": cg.g.nodes[r.node]["qualname"],
                "score": r.score,
                "distance": r.distance,
                "reasons": list(r.reasons),
            }
            for r in br.ranked[:limit]
        ],
        "stats": br.stats,
    }


def resolve_issue(
    repo: str,
    issue: str,
    mode: str = "graph",
    k: int = 12,
    budget_tokens: int = 8000,
) -> dict[str, Any]:
    return _resolve_issue(repo, issue, mode=mode, k=k, budget_tokens=budget_tokens)


def blast_radius(
    repo: str,
    issue: str,
    hops: int = 3,
    min_confidence: float = 0.0,
    limit: int = 20,
) -> dict[str, Any]:
    return _blast_radius_query(repo, issue, hops=hops, min_confidence=min_confidence, limit=limit)


def build_server() -> MCPServer:
    """Constructs the server and registers both tools. Imports the `mcp`
    package here, not at module level -- see the module docstring."""
    from mcp.server.mcpserver import MCPServer as _MCPServer

    server = _MCPServer(name="cartographer", version="0.1.0", instructions=INSTRUCTIONS)
    server.tool(
        name="resolve_issue",
        description=(
            "Graph-grounded context for a SWE-style issue: ranked files and "
            "code snippets under a token budget, using a real dependency "
            "graph, not embeddings. Does NOT call a model and does NOT "
            "produce a patch -- returns the material an agent needs to do "
            "the fix itself."
        ),
    )(resolve_issue)
    server.tool(
        name="blast_radius",
        description=(
            "Raw graph-grounded ranking for a SWE-style issue: which symbols "
            "and files the dependency graph reaches from the issue's own "
            "seeds, and why (confidence-weighted call/import/inherit edges, "
            "hop distance). Lower-level than resolve_issue -- no snippets, "
            "no token budget, just the ranking and its provenance."
        ),
    )(blast_radius)
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
