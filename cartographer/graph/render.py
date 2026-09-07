"""Render a built graph as a module-level dependency diagram.

Symbol granularity is right for retrieval and useless for a picture -- 46554
nodes on django. This collapses to files, keeps only `imports` edges, and drops
edges a package's `__init__` re-export creates but nobody would draw.

Mermaid rather than an image because it renders on GitHub with no build step
and stays a diff a human can read.
"""

from __future__ import annotations

from collections import defaultdict

from .graph_builder import MODULE_SYMBOL, CodeGraph

_MAX_LABEL = 34


def _module_edges(cg: CodeGraph) -> dict[tuple[str, str], int]:
    edges: dict[tuple[str, str], int] = defaultdict(int)
    for u, v, key in cg.g.edges(keys=True):
        if key != "imports":
            continue
        a, b = cg.g.nodes[u]["path"], cg.g.nodes[v]["path"]
        if a != b:
            edges[(a, b)] += 1
    return dict(edges)


def _group(path: str) -> str:
    """Top two path segments, so a package reads as one cluster."""
    parts = path.split("/")
    return "/".join(parts[:-1]) or "."


def _node_id(path: str, seen: dict[str, str]) -> str:
    if path not in seen:
        seen[path] = f"n{len(seen)}"
    return seen[path]


def _excluded(path: str, patterns: tuple[str, ...]) -> bool:
    """Match whole path segments, never bare substrings.

    `--exclude eval` once silently removed the entire `retrieval` package from
    the diagram, because "retrieval" ends in "eval". A filter that quietly drops
    the most important package in the repo is worse than no filter.
    """
    segments = path.split("/")
    stem = segments[-1].removesuffix(".py")
    return any(p in segments or p == stem for p in patterns)


def _label(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name == "__init__.py":
        name = path.split("/")[-2] + "/"
    name = name.removesuffix(".py")
    return name if len(name) <= _MAX_LABEL else name[: _MAX_LABEL - 1] + "…"


def to_mermaid(cg: CodeGraph, *, exclude: tuple[str, ...] = (), max_nodes: int = 60) -> str:
    """A `graph LR` diagram of module imports, clustered by directory.

    `max_nodes` is a refusal, not a truncation: a diagram of the 200 busiest
    files in django is not a diagram, and silently drawing a slice of one would
    misrepresent the repo. Callers get a clear error instead.
    """
    edges = _module_edges(cg)
    if exclude:
        edges = {
            (a, b): w
            for (a, b), w in edges.items()
            if not (_excluded(a, exclude) or _excluded(b, exclude))
        }

    paths = sorted({p for pair in edges for p in pair})
    if len(paths) > max_nodes:
        raise ValueError(
            f"{len(paths)} modules exceeds max_nodes={max_nodes}; "
            "narrow with --exclude or raise the cap deliberately"
        )
    if not paths:
        return "graph LR\n  empty[no internal imports found]\n"

    ids: dict[str, str] = {}
    by_group: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        by_group[_group(p)].append(p)

    out = ["graph LR"]
    for gi, (group, members) in enumerate(sorted(by_group.items())):
        out.append(f'  subgraph g{gi}["{group}"]')
        for p in sorted(members):
            out.append(f'    {_node_id(p, ids)}["{_label(p)}"]')
        out.append("  end")
    for (a, b), _ in sorted(edges.items()):
        out.append(f"  {ids[a]} --> {ids[b]}")
    return "\n".join(out) + "\n"


def to_dot(cg: CodeGraph) -> str:
    edges = _module_edges(cg)
    ids: dict[str, str] = {}
    lines = ["digraph modules {", "  rankdir=LR;", '  node [shape=box, fontname="monospace"];']
    for p in sorted({p for pair in edges for p in pair}):
        lines.append(f'  {_node_id(p, ids)} [label="{_label(p)}"];')
    for a, b in sorted(edges):
        lines.append(f"  {ids[a]} -> {ids[b]};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def module_summary(cg: CodeGraph) -> list[tuple[str, int, int]]:
    """(path, fan_in, fan_out) over module imports, most depended-on first.

    The fan-in/fan-out pair the ranking already uses, surfaced so a human can
    sanity-check the graph against their own sense of the codebase.
    """
    fan_in: dict[str, int] = defaultdict(int)
    fan_out: dict[str, int] = defaultdict(int)
    for a, b in _module_edges(cg):
        fan_out[a] += 1
        fan_in[b] += 1
    paths = {n["path"] for _, n in cg.g.nodes(data=True) if n.get("qualname") == MODULE_SYMBOL}
    return sorted(
        ((p, fan_in[p], fan_out[p]) for p in paths),
        key=lambda r: (-r[1], -r[2], r[0]),
    )
