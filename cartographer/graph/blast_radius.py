"""Seeds -> ranked impacted subgraph.

## The direction asymmetry

The single most important choice here: **callers matter more than callees.** An
issue names a symptom -- a function that misbehaves -- and the fix usually lives
at or above it, in whoever passes it the wrong thing or handles its result
wrong. Callees are context for understanding, but they are rarely where the
patch goes. So a `calls` edge is worth roughly twice as much traversed
backwards (seed <- caller) as forwards.

## Why mass is normalised by degree

Contribution is split across the neighbours it spreads to, PageRank-style,
rather than copied to each. Without that, a seed calling a 300-caller utility
floods 300 nodes with full mass on hop one and the ranking becomes a list of
the repo's most popular functions -- true of every issue, useful for none. The
normalisation is also where the fan-in/fan-out intuition enters: a hub absorbs
mass from many contributors, so it still ranks when it is genuinely central,
but it cannot broadcast.

Mass is accumulated over *all* paths within the hop budget rather than taking a
single shortest path. Two independent routes from the seeds to a node are real
evidence that it sits in the blast radius, and a shortest-path score throws the
second one away.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .graph_builder import MODULE_SYMBOL, CodeGraph

#: Per-relation weights, as (backward, forward) from the perspective of the
#: node currently holding mass. "backward" means traversing an edge against its
#: direction, i.e. towards whoever depends on this node.
RELATION_WEIGHTS: dict[str, tuple[float, float]] = {
    # Callers are where fixes go; callees are context.
    "calls": (1.0, 0.45),
    # A subclass is strongly affected by its base. The reverse -- a base
    # affected by one of its subclasses -- is weaker but real.
    "inherits": (0.8, 0.55),
    # Whole-module granularity: real dependency signal, but coarse, so cheap.
    "imports": (0.30, 0.22),
    # Siblings and the enclosing class. Symmetric: a method's class is as
    # relevant to it as it is to the class.
    "contains": (0.5, 0.5),
}

DEFAULT_DECAY = 0.55
DEFAULT_HOPS = 3


@dataclass(frozen=True, slots=True)
class Ranked:
    node: str
    score: float
    distance: int
    #: How this node was reached, most-significant first. Provenance is not
    #: decoration -- it is what a Snippet's `reason` carries, and the only way a
    #: human can tell a graph hit from a lucky name match.
    reasons: tuple[str, ...] = ()

    @property
    def is_seed(self) -> bool:
        return self.distance == 0


@dataclass(slots=True)
class BlastRadius:
    ranked: list[Ranked]
    seeds: tuple[str, ...]
    stats: dict[str, float | int] = field(default_factory=dict)

    def top(self, k: int) -> list[Ranked]:
        return self.ranked[:k]

    def files(self, k: int, cg: CodeGraph) -> list[str]:
        out: dict[str, None] = {}
        for r in self.top(k):
            out.setdefault(cg.g.nodes[r.node]["path"], None)
        return list(out)


def _neighbours(cg: CodeGraph, node: str, min_confidence: float):
    """Every traversable step out of `node` as (neighbour, relation, backward,
    confidence). Backward edges are yielded first only for readability; order
    does not affect the score."""
    g = cg.g
    for u, _, key, data in g.in_edges(node, keys=True, data=True):
        conf = data.get("confidence", 1.0)
        if key == "calls" and conf < min_confidence:
            continue
        yield u, key, True, conf
    for _, v, key, data in g.out_edges(node, keys=True, data=True):
        conf = data.get("confidence", 1.0)
        if key == "calls" and conf < min_confidence:
            continue
        yield v, key, False, conf


def blast_radius(
    cg: CodeGraph,
    seeds: list[str],
    *,
    hops: int = DEFAULT_HOPS,
    decay: float = DEFAULT_DECAY,
    min_confidence: float = 0.0,
    seed_weights: dict[str, float] | None = None,
    include_modules: bool = False,
) -> BlastRadius:
    """Rank the symbols a fix for `seeds` is likely to have to touch.

    `min_confidence` filters `calls` edges by how they were resolved, so the
    eval can ablate the weak name-match tiers without rebuilding the graph --
    the question "does the guessed half of the graph help or hurt?" is one the
    comparison should answer with a number rather than an opinion.
    """
    g = cg.g
    live = [s for s in seeds if s in g]
    if not live:
        return BlastRadius(ranked=[], seeds=(), stats={"seeds_found": 0, "seeds_given": len(seeds)})

    weights = seed_weights or {}
    score: dict[str, float] = defaultdict(float)
    distance: dict[str, int] = {}
    reasons: dict[str, list[tuple[float, str]]] = defaultdict(list)

    frontier: dict[str, float] = {}
    for s in live:
        w = weights.get(s, 1.0)
        frontier[s] = frontier.get(s, 0.0) + w
        score[s] += w
        distance[s] = 0
        reasons[s].append((w, "seed"))

    visits = 0
    for hop in range(1, hops + 1):
        nxt: dict[str, float] = defaultdict(float)
        for node, mass in frontier.items():
            steps = list(_neighbours(cg, node, min_confidence))
            if not steps:
                continue
            # Split, don't copy: see the module docstring. Without this a single
            # popular utility floods the ranking on hop one.
            share = mass / len(steps)
            for nb, rel, backward, conf in steps:
                if not include_modules and g.nodes[nb].get("qualname") == MODULE_SYMBOL:
                    continue
                # Mass flows outward only. Without this a 2-cycle (seed -> x ->
                # seed) reads as a second independent path and inflates the
                # score of whatever the seeds are most tangled with. Distinct
                # paths arriving at the same depth still accumulate, which is
                # the evidence we actually wanted.
                prior = distance.get(nb)
                if prior is not None and prior < hop:
                    continue
                back_w, fwd_w = RELATION_WEIGHTS.get(rel, (0.2, 0.2))
                contrib = share * decay * (back_w if backward else fwd_w) * conf
                if contrib <= 1e-6:
                    continue
                visits += 1
                score[nb] += contrib
                nxt[nb] += contrib
                distance.setdefault(nb, hop)
                reasons[nb].append(
                    (contrib, f"{rel} {'from' if backward else 'to'} {_short(g, node)} @{hop}")
                )
        if not nxt:
            break
        frontier = dict(nxt)

    ranked = sorted(
        (
            Ranked(
                node=n,
                score=round(s, 6),
                distance=distance.get(n, hops + 1),
                reasons=tuple(r for _, r in sorted(reasons[n], key=lambda x: -x[0])[:3]),
            )
            for n, s in score.items()
        ),
        # Seeds first, then score. A seed the issue named outranks anything
        # inferred about it, however heavily connected.
        key=lambda r: (r.distance != 0, -r.score, r.node),
    )
    return BlastRadius(
        ranked=ranked,
        seeds=tuple(live),
        stats={
            "seeds_given": len(seeds),
            "seeds_found": len(live),
            "reached": len(ranked),
            "expansions": visits,
            "hops": hops,
        },
    )


def _short(g, node: str) -> str:
    d = g.nodes[node]
    return f"{d['path']}:{d['qualname']}"
