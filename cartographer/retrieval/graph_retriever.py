"""Graph-grounded retriever -- the contribution.

Pipeline: issue text -> lexical seeds -> blast radius -> snippets under a token
budget.

## Why the issue's own file mentions are pinned in front

Measured over 59 SWE-bench Verified instances (requests, pytest, pylint,
xarray), file-level recall:

    k     graph-only   union   seeds-only
    1        0.237     0.288     0.288
    3        0.421     0.438     0.438
    5        0.506     0.523     0.514
    10       0.582     0.582     0.556
    20       0.740     0.740     0.556

Ranking purely by graph score was *worse than its own seeds* at small k: an
issue that names `sklearn/linear_model/base.py` has told us the answer, and a
symbol-level score can dilute that behind neighbours it inferred. So the order
is evidence-first -- files the issue literally names, then everything the graph
inferred. The union is >= both at every k, which is the property that matters:
the retriever must never do worse than the regex it is built on top of.

The win is real but concentrated at wide k (+18 points at 20 vs seeds alone).
That is a weaker claim than the thesis wants and it is recorded here rather
than rounded off; closing the gap at small k is open work.
"""

from __future__ import annotations

from ..graph.blast_radius import blast_radius
from ..graph.graph_builder import MODULE_SYMBOL, CodeGraph, build_graph, node_id
from .base import Context, Issue, RepoRef, Snippet
from .seeds import extract_seeds, seed_files

#: A single symbol may not eat the whole budget. Long classes are truncated to
#: their head, which holds the signature, the docstring and the first methods --
#: the part that tells a model what it is looking at.
MAX_SNIPPET_LINES = 80

#: Rough chars-per-token. Only ever used for budgeting, never for billing.
CHARS_PER_TOKEN = 4


def _read(cg: CodeGraph, path: str) -> list[str]:
    try:
        return (cg.root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


class GraphRetriever:
    mode = "graph"

    def __init__(
        self,
        *,
        k: int = 12,
        max_hops: int = 3,
        min_confidence: float = 0.0,
        max_snippet_lines: int = MAX_SNIPPET_LINES,
        graph: CodeGraph | None = None,
    ) -> None:
        self.k = k
        self.max_hops = max_hops
        self.min_confidence = min_confidence
        self.max_snippet_lines = max_snippet_lines
        # Injectable so an eval sweep can build one graph and reuse it across
        # configurations instead of re-parsing the repo per run.
        self._graph = graph

    def _build(self, repo: RepoRef) -> CodeGraph:
        if self._graph is not None:
            return self._graph
        return build_graph(repo.root)

    def rank(self, issue: Issue, cg: CodeGraph) -> tuple[list[str], dict]:
        """Ordered node ids, evidence first. Separated from snippet building so
        the eval can measure the ranking without paying to read files."""
        text = issue.text
        seeds = extract_seeds(text, cg)
        weights = {s.node: s.weight for s in seeds}
        br = blast_radius(
            cg, list(weights), hops=self.max_hops,
            min_confidence=self.min_confidence, seed_weights=weights,
        )

        best_in_file: dict[str, str] = {}
        for r in br.ranked:
            best_in_file.setdefault(cg.g.nodes[r.node]["path"], r.node)

        order: list[str] = []
        seen: set[str] = set()

        def offer(node: str) -> None:
            if node in cg.g and node not in seen:
                seen.add(node)
                order.append(node)

        # Evidence first: a file the issue names outranks anything inferred.
        for path in seed_files(text, cg):
            offer(best_in_file.get(path) or node_id(path, MODULE_SYMBOL))
        for r in br.ranked:
            offer(r.node)

        stats = {
            "seeds": len(seeds),
            "seed_files": len(seed_files(text, cg)),
            "reached": len(br.ranked),
            "graph_nodes": cg.stats.get("nodes", 0),
            "graph_files": cg.stats.get("files", 0),
            "build_seconds": cg.stats.get("build_seconds", 0.0),
            "hops": self.max_hops,
            "min_confidence": self.min_confidence,
        }
        return order, stats

    def retrieve(self, issue: Issue, repo: RepoRef, *, budget_tokens: int = 8000) -> Context:
        cg = self._build(repo)
        order, stats = self.rank(issue, cg)

        snippets: list[Snippet] = []
        used = 0
        truncated = 0
        for rank, node in enumerate(order, start=1):
            if len(snippets) >= self.k or used >= budget_tokens:
                break
            d = cg.g.nodes[node]
            lines = _read(cg, d["path"])
            if not lines:
                continue
            start = max(1, d["start_line"])
            end = min(len(lines), d["end_line"])
            if end < start:
                continue
            if end - start + 1 > self.max_snippet_lines:
                end = start + self.max_snippet_lines - 1
                truncated += 1
            text = "\n".join(lines[start - 1 : end])
            cost = len(text) // CHARS_PER_TOKEN
            # Never emit a snippet that would blow the budget, but always emit
            # at least one: a context of nothing is strictly worse than a
            # context of the single best-ranked symbol.
            if snippets and used + cost > budget_tokens:
                continue
            snippets.append(
                Snippet(
                    path=d["path"],
                    start_line=start,
                    end_line=end,
                    text=text,
                    score=1.0 - len(snippets) / max(self.k, 1),
                    symbol=None if d["qualname"] == MODULE_SYMBOL else d["qualname"],
                    reason=f"graph rank {rank}",
                )
            )
            used += cost

        return Context(
            snippets=tuple(snippets),
            mode=self.mode,
            token_estimate=used,
            stats={**stats, "truncated_snippets": truncated, "k": self.k},
        )
