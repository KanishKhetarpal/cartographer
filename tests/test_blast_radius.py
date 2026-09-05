"""Blast-radius ranking contract.

The properties pinned here are the ones the retrieval quality actually rests
on: seeds outrank inference, callers outrank callees, and a hub cannot
broadcast. Absolute scores are deliberately never asserted -- they are tuning,
and freezing them would make every future weight change a test rewrite.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.blast_radius import DEFAULT_DECAY, RELATION_WEIGHTS, blast_radius
from cartographer.graph.graph_builder import MODULE_SYMBOL, build_graph, node_id


def write(root, files: dict[str, str]):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).strip() + "\n", encoding="utf-8")
    return root


def rank_of(br, node: str) -> int:
    for i, r in enumerate(br.ranked):
        if r.node == node:
            return i
    raise AssertionError(f"{node} not in ranking: {[r.node for r in br.ranked]}")


@pytest.fixture
def chain(tmp_path):
    """caller -> middle -> leaf, all in separate files."""
    return build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/leaf.py": "def leaf():\n    pass\n",
                "pkg/middle.py": (
                    "from pkg.leaf import leaf\n\n\ndef middle():\n    return leaf()\n"
                ),
                "pkg/caller.py": (
                    "from pkg.middle import middle\n\n\ndef caller():\n    return middle()\n"
                ),
            },
        )
    )


def test_a_seed_outranks_everything_inferred_about_it(chain):
    seed = node_id("pkg/middle.py", "middle")
    br = blast_radius(chain, [seed])
    assert br.ranked[0].node == seed
    assert br.ranked[0].is_seed and br.ranked[0].reasons == ("seed",)


def test_callers_outrank_callees(chain):
    """The fix for a misbehaving function usually lives at or above it. This is
    the single most consequential weighting in the module."""
    br = blast_radius(chain, [node_id("pkg/middle.py", "middle")])
    caller, leaf = node_id("pkg/caller.py", "caller"), node_id("pkg/leaf.py", "leaf")
    assert rank_of(br, caller) < rank_of(br, leaf)
    assert RELATION_WEIGHTS["calls"][0] > RELATION_WEIGHTS["calls"][1]


def test_score_decays_with_distance(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "def a():\n    pass\n",
                "pkg/b.py": "from pkg.a import a\n\n\ndef b():\n    return a()\n",
                "pkg/c.py": "from pkg.b import b\n\n\ndef c():\n    return b()\n",
            },
        )
    )
    br = blast_radius(cg, [node_id("pkg/a.py", "a")])
    near = next(r for r in br.ranked if r.node == node_id("pkg/b.py", "b"))
    far = next(r for r in br.ranked if r.node == node_id("pkg/c.py", "c"))
    assert near.distance < far.distance
    assert near.score > far.score


def test_hops_bounds_the_traversal(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "def a():\n    pass\n",
                "pkg/b.py": "from pkg.a import a\n\n\ndef b():\n    return a()\n",
                "pkg/c.py": "from pkg.b import b\n\n\ndef c():\n    return b()\n",
            },
        )
    )
    reached = {r.node for r in blast_radius(cg, [node_id("pkg/a.py", "a")], hops=1).ranked}
    assert node_id("pkg/b.py", "b") in reached
    assert node_id("pkg/c.py", "c") not in reached


def test_a_hub_cannot_broadcast(tmp_path):
    """A seed that calls a 40-caller utility must not flood the ranking with all
    40 of its other callers -- that is a list of the repo's popular functions,
    true of every issue and useful for none."""
    files = {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper():\n    pass\n",
        "pkg/seed.py": "from pkg.util import helper\n\n\ndef seed():\n    return helper()\n",
    }
    for i in range(40):
        files[f"pkg/u{i}.py"] = (
            f"from pkg.util import helper\n\n\ndef caller{i}():\n    return helper()\n"
        )
    cg = build_graph(write(tmp_path, files))
    br = blast_radius(cg, [node_id("pkg/seed.py", "seed")], hops=2)

    util = next(r for r in br.ranked if r.node == node_id("pkg/util.py", "helper"))
    others = [r for r in br.ranked if r.node.startswith("pkg/u") and r.node[5].isdigit()]
    assert others, "the hub's other callers should be reachable at all"
    # Each sibling gets a fortieth of what the hub passes on, so the hub itself
    # stays far above them rather than dragging them all up with it.
    assert util.score > 10 * max(r.score for r in others)


def test_min_confidence_excludes_weakly_resolved_call_edges(tmp_path):
    """`min_confidence` is what lets the eval ablate the guessed half of the
    graph without rebuilding it."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "m.py": "def f(obj):\n    obj.singular_name()\n",
                "other.py": "class K:\n    def singular_name(self):\n        pass\n",
            },
        )
    )
    target = node_id("other.py", "K.singular_name")
    assert target in {r.node for r in blast_radius(cg, [node_id("m.py", "f")]).ranked}
    strict = blast_radius(cg, [node_id("m.py", "f")], min_confidence=0.8)
    assert target not in {r.node for r in strict.ranked}


def test_module_nodes_are_excluded_unless_asked_for(chain):
    seed = node_id("pkg/middle.py", "middle")
    plain = {r.node for r in blast_radius(chain, [seed]).ranked}
    assert not any(n.endswith(MODULE_SYMBOL) for n in plain)
    withmods = {r.node for r in blast_radius(chain, [seed], include_modules=True).ranked}
    assert any(n.endswith(MODULE_SYMBOL) for n in withmods)


def test_unknown_seeds_are_reported_not_silently_dropped(chain):
    br = blast_radius(chain, ["nowhere.py::ghost"])
    assert br.ranked == [] and br.stats["seeds_found"] == 0 and br.stats["seeds_given"] == 1


def test_seed_weights_are_honoured(chain):
    a, b = node_id("pkg/leaf.py", "leaf"), node_id("pkg/caller.py", "caller")
    br = blast_radius(chain, [a, b], seed_weights={b: 5.0})
    assert br.ranked[0].node == b


def test_reasons_record_how_a_node_was_reached(chain):
    br = blast_radius(chain, [node_id("pkg/middle.py", "middle")])
    caller = next(r for r in br.ranked if r.node == node_id("pkg/caller.py", "caller"))
    assert caller.reasons and "calls from" in caller.reasons[0]


def test_decay_is_what_separates_near_from_far(tmp_path):
    """The distance test above passes on degree-splitting alone, so decay needs
    isolating: raising it must lift the distance-2 node relative to the
    distance-1 node, and nothing else in the walk changes."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "def a():\n    pass\n",
                "pkg/b.py": "from pkg.a import a\n\n\ndef b():\n    return a()\n",
                "pkg/c.py": "from pkg.b import b\n\n\ndef c():\n    return b()\n",
            },
        )
    )
    seed = node_id("pkg/a.py", "a")

    def ratio(decay: float) -> float:
        br = blast_radius(cg, [seed], decay=decay)
        by = {r.node: r.score for r in br.ranked}
        return by[node_id("pkg/c.py", "c")] / by[node_id("pkg/b.py", "b")]

    assert ratio(0.2) < ratio(0.55) < ratio(0.9)
    # The exact default is tuning and is deliberately not frozen here, but
    # distance has to cost something or the walk is a plain reachability set.
    assert 0 < DEFAULT_DECAY < 1


def test_a_seed_stays_first_even_when_an_inferred_node_outscores_it(chain):
    """Seed pinning is a separate rule from scoring. Lowering one seed's weight
    is not enough to show it -- everything downstream scales with it -- so this
    uses a heavy seed whose neighbourhood outscores a second, near-weightless
    seed the issue also named."""
    heavy = node_id("pkg/leaf.py", "leaf")
    faint = node_id("pkg/caller.py", "caller")
    br = blast_radius(chain, [faint, heavy], seed_weights={faint: 0.001, heavy: 10.0})
    assert br.ranked[0].is_seed
    inferred = [r for r in br.ranked if not r.is_seed]
    faint_score = next(r.score for r in br.ranked if r.node == faint)
    assert any(r.score > faint_score for r in inferred), (
        "test is vacuous unless something inferred really does outscore a seed"
    )
    assert [r.node for r in br.ranked[:2]] == sorted([heavy, faint], key=lambda n: n != heavy)
