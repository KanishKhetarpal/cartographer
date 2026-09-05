"""Graph-grounded retriever -- the contribution.

PHASE 0 STUB. The wiring is real; the intelligence is not. It walks the repo and
returns the head of the first few source files so the CLI can be exercised end
to end before the graph engine exists.

Every stub context carries `stats["stub"] = True`. The eval harness refuses to
score a run whose contexts are stubbed, so a Phase-0 placeholder can never be
mistaken for a result.
"""

from __future__ import annotations

from .base import Context, Issue, RepoRef, Snippet
from .stub import stub_snippets


class GraphRetriever:
    mode = "graph"

    def __init__(self, *, k: int = 12, max_hops: int = 3) -> None:
        self.k = k
        self.max_hops = max_hops

    def retrieve(self, issue: Issue, repo: RepoRef, *, budget_tokens: int = 8000) -> Context:
        snippets: tuple[Snippet, ...] = stub_snippets(repo, self.k, reason="stub: graph mode")
        return Context(
            snippets=snippets,
            mode=self.mode,
            token_estimate=sum(len(s.text) for s in snippets) // 4,
            stats={"stub": True, "k": self.k, "max_hops": self.max_hops},
        )
