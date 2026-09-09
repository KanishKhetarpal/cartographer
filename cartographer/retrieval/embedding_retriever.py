"""Embedding baseline -- the control.

Deliberately vanilla, per the project brief: chunk the repo into fixed-size
line windows, embed each chunk and the issue text with a standard sentence
embedding model, rank by cosine similarity, take the top k. No reranking, no
query expansion, no hybrid lexical fusion -- every one of those would be a
second contribution riding along inside what is supposed to be the plain
control. A win over a *good* embedding baseline means something; a win over a
crippled one does not.

Model: `sentence-transformers/all-MiniLM-L6-v2` -- small, fast on CPU, and the
most widely used default in this space, which is the point: the baseline
should be the thing a reasonable engineer reaches for first, not a strawman
tuned to lose.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from .base import Context, Issue, RepoRef, Snippet

if TYPE_CHECKING:
    import numpy as np

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist",
}

CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Chunk:
    path: str
    start_line: int
    end_line: int
    text: str


@lru_cache(maxsize=2)
def _load_model(name: str):
    # Cached at module scope: an eval sweep constructs a fresh
    # EmbeddingRetriever per instance the same way it does for the graph
    # retriever, and reloading ~90MB of weights per instance would dominate
    # wall time for no reason -- the model is the same across instances, only
    # the repo checkout and the issue text change.
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name)


def chunk_repo(root: Path, *, chunk_lines: int = 40) -> list[Chunk]:
    """Every `.py` file, split into non-overlapping fixed-size windows.

    No overlap and no smarter boundary (function/class) on purpose -- a chunker
    that already understood code structure would be smuggling the graph's idea
    into the baseline it exists to be measured against.
    """
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        rel = path.relative_to(root).as_posix()
        for start in range(0, len(lines), chunk_lines):
            window = lines[start : start + chunk_lines]
            text = "\n".join(window)
            if not text.strip():
                continue
            chunks.append(
                Chunk(
                    path=rel,
                    start_line=start + 1,
                    end_line=start + len(window),
                    text=text,
                )
            )
    return chunks


class EmbeddingRetriever:
    mode = "embedding"

    def __init__(
        self,
        *,
        k: int = 12,
        chunk_lines: int = 40,
        model_name: str = DEFAULT_MODEL,
        index: tuple[list[Chunk], np.ndarray] | None = None,
    ) -> None:
        self.k = k
        self.chunk_lines = chunk_lines
        self.model_name = model_name
        # Same injection pattern as GraphRetriever(graph=...): an eval sweep
        # embeds a repo once per checkout and reuses it across instances that
        # share that checkout, rather than re-embedding thousands of chunks
        # per issue.
        self._index = index

    def _build_index(self, repo: RepoRef) -> tuple[list[Chunk], np.ndarray]:
        if self._index is not None:
            return self._index
        chunks = chunk_repo(repo.root, chunk_lines=self.chunk_lines)
        model = _load_model(self.model_name)
        if not chunks:
            return chunks, model.encode([], normalize_embeddings=True)
        vectors = model.encode(
            [c.text for c in chunks],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
            batch_size=64,
        )
        return chunks, vectors

    def rank(self, issue: Issue, repo: RepoRef) -> tuple[list[Chunk], np.ndarray, np.ndarray]:
        """Every chunk, ranked by cosine similarity to the issue -- unfiltered
        by `k` or `budget_tokens`. Separated from `retrieve()` the same way
        `GraphRetriever.rank()` is: an eval measuring file-level recall needs
        the full order (many chunks can share one file, so a top-k slice of
        *chunks* can silently omit files a top-k slice of *files* would have
        kept), while `retrieve()` alone answers "what does the agent see."

        Returns `(chunks, scores, order)` -- `order` is `scores` sorted
        descending, as chunk indices.
        """
        import numpy as np

        chunks, vectors = self._build_index(repo)
        if not chunks:
            return chunks, np.array([]), np.array([], dtype=int)

        model = _load_model(self.model_name)
        query = model.encode(
            [issue.text], normalize_embeddings=True, convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
        scores = vectors @ query  # cosine, since both sides are L2-normalised
        return chunks, scores, np.argsort(-scores)

    def retrieve(self, issue: Issue, repo: RepoRef, *, budget_tokens: int = 8000) -> Context:
        chunks, scores, order = self.rank(issue, repo)
        if not chunks:
            return Context(mode=self.mode, stats={"chunks": 0})

        snippets: list[Snippet] = []
        used = 0
        for pos, idx in enumerate(order, start=1):
            if len(snippets) >= self.k or used >= budget_tokens:
                break
            c = chunks[int(idx)]
            cost = len(c.text) // CHARS_PER_TOKEN
            if snippets and used + cost > budget_tokens:
                continue
            snippets.append(
                Snippet(
                    path=c.path,
                    start_line=c.start_line,
                    end_line=c.end_line,
                    text=c.text,
                    score=float(scores[idx]),
                    reason=f"embedding rank {pos}, cosine={scores[idx]:.3f}",
                )
            )
            used += cost

        return Context(
            snippets=tuple(snippets),
            mode=self.mode,
            token_estimate=used,
            stats={
                "chunks": len(chunks),
                "k": self.k,
                "chunk_lines": self.chunk_lines,
                "model": self.model_name,
            },
        )
