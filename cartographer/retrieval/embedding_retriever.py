"""Embedding baseline -- the control.

Deliberately vanilla: chunk the repo, embed, top-k by cosine. It is not meant to
be a good retriever, it is meant to be the *standard* one, so that a win over it
is attributable to the graph rather than to effort spent here.

PHASE 0 STUB -- see `graph_retriever` for the stub contract.
"""

from __future__ import annotations

from .base import Context, Issue, RepoRef, Snippet
from .stub import stub_snippets


class EmbeddingRetriever:
    mode = "embedding"

    def __init__(self, *, k: int = 12, chunk_lines: int = 40) -> None:
        self.k = k
        self.chunk_lines = chunk_lines

    def retrieve(self, issue: Issue, repo: RepoRef, *, budget_tokens: int = 8000) -> Context:
        snippets: tuple[Snippet, ...] = stub_snippets(repo, self.k, reason="stub: embedding mode")
        return Context(
            snippets=snippets,
            mode=self.mode,
            token_estimate=sum(len(s.text) for s in snippets) // 4,
            stats={"stub": True, "k": self.k, "chunk_lines": self.chunk_lines},
        )
