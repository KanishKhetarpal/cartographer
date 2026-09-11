"""Graph-grounded retriever -- the contribution.

Pipeline: issue text -> lexical seeds -> blast radius -> snippets under a token
budget.

## Why the issue's own file mentions are pinned in front

Ranking purely by graph score was *worse than its own seeds* at small k. An
issue that names `sklearn/linear_model/base.py` has told us the answer, and a
symbol-level score dilutes that behind neighbours it inferred. Measured at the
time on 59 SWE-bench Verified instances (2026-09-06, before the path-matching
fix), file recall at k=1 was 0.237 ranking by score against 0.288 for the seeds
alone; the union of the two was >= both at every k.

So the order is evidence-first -- files the issue literally names, then
everything the graph inferred. **The property to preserve is that the retriever
never does worse than the regex it is built on top of**, not those particular
numbers.

Current standing, same 59 instances (`results/phase1_hitrate.json`):

    k        graph   seeds-only
    1        0.356     0.356
    3        0.557     0.557
    5        0.616     0.599
    10       0.684     0.633
    20       0.774     0.633

The graph contributes nothing at k<=3, where the issue's own text is already
the whole answer, and +5 to +14 points from k=5 out. That is a weaker claim
than a thesis about tight context wants, and it is recorded rather than rounded
off.
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
        original_rank = {node: i for i, node in enumerate(order, start=1)}

        # File-diversity pass: take each file's best-ranked node first, in rank
        # order, before a second node from any file already covered. Otherwise
        # a file with several highly-ranked symbols can spend all of k before a
        # second file is considered at all -- measured on 59 real instances
        # (eval/probe_snippet_diversity.py) to cost ~6 points of delivered file
        # recall at the default k=12 and to drop the gold file entirely for
        # 5/59, all cases where the unbounded ranking had already reached it
        # within the same k, just spread across nodes the snippet budget never
        # got to. This does not change *which* node represents a given file --
        # that is still whichever ranks first for that path in `order` -- only
        # the order files are covered in.
        diversified: list[str] = []
        leftover: list[str] = []
        seen_files: set[str] = set()
        for node in order:
            path = cg.g.nodes[node]["path"]
            if path in seen_files:
                leftover.append(node)
            else:
                seen_files.add(path)
                diversified.append(node)
        diversified.extend(leftover)

        snippets: list[Snippet] = []
        used = 0
        truncated = 0
        for node in diversified:
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
                    reason=f"graph rank {original_rank[node]}",
                )
            )
            used += cost

        return Context(
            snippets=tuple(snippets),
            mode=self.mode,
            token_estimate=used,
            stats={**stats, "truncated_snippets": truncated, "k": self.k},
        )
