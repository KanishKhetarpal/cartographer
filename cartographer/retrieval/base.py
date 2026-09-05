"""Retrieval interface.

The whole experiment rests on this file: `GraphRetriever` and `EmbeddingRetriever`
must be indistinguishable to the agent loop. Anything a retriever knows that the
loop can branch on is a confound in the eval, so the only channel out of a
retriever is `Context`, and `Context.mode`/`Context.stats` are for *reporting*,
never for control flow inside the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Issue:
    """A task to resolve. Mirrors the fields SWE-bench actually gives us."""

    id: str
    title: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A checkout to reason about."""

    root: Path
    base_commit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))


@dataclass(frozen=True, slots=True)
class Snippet:
    """One contiguous span of source offered to the model.

    `start_line`/`end_line` are 1-based and inclusive, matching every tool a
    human would cross-check this against (git, tracebacks, editors).
    """

    path: str  # repo-relative, forward slashes
    start_line: int
    end_line: int
    text: str
    score: float
    symbol: str | None = None
    reason: str = ""  # provenance, e.g. "call-edge distance 2 from seed"

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class Context:
    """What a retriever hands the agent. Identical shape for every mode."""

    snippets: tuple[Snippet, ...] = ()
    mode: str = "unknown"
    token_estimate: int = 0
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def files(self) -> tuple[str, ...]:
        """Distinct paths, in first-appearance (i.e. rank) order."""
        seen: dict[str, None] = {}
        for s in self.snippets:
            seen.setdefault(s.path, None)
        return tuple(seen)


@runtime_checkable
class Retriever(Protocol):
    """Given an issue and a repo, return the minimal context to fix it."""

    mode: str

    def retrieve(self, issue: Issue, repo: RepoRef, *, budget_tokens: int = 8000) -> Context: ...
