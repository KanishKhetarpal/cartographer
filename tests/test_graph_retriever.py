"""GraphRetriever contract.

The ordering rule and the budget rule are the two things here that changed
behaviour measurably on real instances, so both are pinned. Recall quality is
not tested here -- that is what `eval/` measures against real repos, and a unit
test asserting a recall number would be fitting to a fixture.

Several of these carry an explicit "test is vacuous unless ..." assertion. That
is not decoration: the first cut of this file had three tests that passed with
the behaviour they name deleted, because the fixture was too small for the rule
to bind.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.blast_radius import blast_radius
from cartographer.graph.graph_builder import build_graph, node_id
from cartographer.retrieval.base import Issue, RepoRef
from cartographer.retrieval.graph_retriever import GraphRetriever
from cartographer.retrieval.seeds import extract_seeds

CALLER_TEMPLATE = """
from pkg.hub import popular


def caller{i}(v):
    return popular(v)
"""


def write(root, files: dict[str, str]):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).strip() + chr(10), encoding="utf-8")
    return root


def hub_repo(root, n_callers: int):
    """One popular symbol, `n_callers` callers, and one file nothing touches."""
    files = {
        "pkg/__init__.py": "",
        "pkg/hub.py": "def popular(v):\n    return v\n",
        "pkg/lonely.py": "def untouched_helper():\n    return 0\n",
    }
    for i in range(n_callers):
        files[f"pkg/c{i}.py"] = CALLER_TEMPLATE.format(i=i)
    return build_graph(write(root, files))


@pytest.fixture
def repo(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/target.py": """
            def broken(value):
                return value + 1
            """,
            "pkg/caller.py": """
            from pkg.target import broken


            def entry(value):
                return broken(value)
            """,
            "pkg/unrelated.py": "def nothing_to_do_here():\n    return 0\n",
        },
    )
    return tmp_path


@pytest.fixture
def cg(repo):
    return build_graph(repo)


def issue(body: str) -> Issue:
    return Issue(id="i", title="", body=body)


# -- ordering --------------------------------------------------------------


def test_a_file_the_issue_names_is_ranked_before_anything_inferred(tmp_path):
    """The retriever must never do worse than the regex it is built on.

    The named file is deliberately the weakest node in the graph -- nothing
    calls it -- while the issue also names a heavily-called symbol, so score
    alone would bury it.
    """
    cg = hub_repo(tmp_path, n_callers=6)
    text = "popular() misbehaves; I think the fix belongs in pkg/lonely.py"

    order, _ = GraphRetriever(graph=cg).rank(issue(text), cg)
    assert cg.g.nodes[order[0]]["path"] == "pkg/lonely.py"

    seeds = extract_seeds(text, cg)
    weights = {s.node: s.weight for s in seeds}
    by_score = blast_radius(cg, list(weights), seed_weights=weights).ranked
    assert cg.g.nodes[by_score[0].node]["path"] != "pkg/lonely.py", (
        "test is vacuous if raw score already ranks the named file first"
    )


def test_the_graph_reaches_a_caller_the_issue_never_mentions(cg):
    """The whole point: `broken()` is named, `entry` is not, and a fix may well
    belong in the caller."""
    order, _ = GraphRetriever(graph=cg).rank(issue("broken() returns one too many"), cg)
    paths = [cg.g.nodes[n]["path"] for n in order]
    assert "pkg/target.py" in paths and "pkg/caller.py" in paths
    assert paths.index("pkg/target.py") < paths.index("pkg/caller.py")


# -- budget and cap --------------------------------------------------------


def test_snippets_are_capped_by_k(tmp_path):
    cg = hub_repo(tmp_path, n_callers=8)
    order, _ = GraphRetriever(graph=cg).rank(issue("popular() is wrong"), cg)
    assert len(order) > 2, "test is vacuous unless more than k nodes are available"

    ctx = GraphRetriever(k=2, graph=cg).retrieve(
        issue("popular() is wrong"), RepoRef(root=tmp_path)
    )
    assert len(ctx.snippets) == 2


def test_a_tiny_budget_still_yields_one_snippet(cg, repo):
    """An empty context is strictly worse than the single best-ranked symbol,
    so the budget bounds the tail rather than the head."""
    ctx = GraphRetriever(k=8, graph=cg).retrieve(
        issue("broken() is wrong"), RepoRef(root=repo), budget_tokens=1
    )
    assert len(ctx.snippets) == 1


def test_the_budget_bounds_the_total(repo):
    filler = chr(10).join(f"    x{i} = {i}" for i in range(400))
    write(repo, {"pkg/target.py": f"def broken(value):{chr(10)}{filler}{chr(10)}    return value"})
    fresh = build_graph(repo)
    ctx = GraphRetriever(k=8, graph=fresh).retrieve(
        issue("broken() is wrong"), RepoRef(root=repo), budget_tokens=200
    )
    assert ctx.token_estimate <= 200 or len(ctx.snippets) == 1


def test_a_long_symbol_is_truncated_to_its_head_not_dropped(repo):
    filler = chr(10).join(f"    x{i} = {i}" for i in range(300))
    write(repo, {"pkg/target.py": f"def broken(value):{chr(10)}{filler}{chr(10)}    return value"})
    fresh = build_graph(repo)
    ctx = GraphRetriever(k=4, max_snippet_lines=10, graph=fresh).retrieve(
        issue("broken() is wrong"), RepoRef(root=repo)
    )
    target = next(s for s in ctx.snippets if s.path == "pkg/target.py")
    assert target.line_count == 10
    assert target.text.splitlines()[0].startswith("def broken")
    assert ctx.stats["truncated_snippets"] >= 1


def test_file_diversity_reaches_a_second_file_within_k(tmp_path):
    """Three symbols named directly in one file rank ahead of a fourth in a
    different file (measured, not assumed -- see
    eval/probe_snippet_diversity.py, which found this costing recall on real
    instances). Without diversification, k=2 would spend entirely inside the
    first file and the second file would never be offered at all."""
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/busy.py": """
            def alpha(v):
                return v


            def beta(v):
                return v


            def gamma(v):
                return v
            """,
            "pkg/quiet.py": "def delta(v):\n    return v\n",
        },
    )
    cg = build_graph(tmp_path)
    text = "alpha() and beta() and gamma() are all wrong, and so is delta()"

    order, _ = GraphRetriever(graph=cg).rank(issue(text), cg)
    paths = [cg.g.nodes[n]["path"] for n in order]
    assert paths[:3] == ["pkg/busy.py"] * 3, (
        "test is vacuous unless the raw ranking already puts three busy.py "
        "symbols ahead of quiet.py's"
    )

    ctx = GraphRetriever(k=2, graph=cg).retrieve(issue(text), RepoRef(root=tmp_path))
    assert {s.path for s in ctx.snippets} == {"pkg/busy.py", "pkg/quiet.py"}


def test_diversification_still_picks_each_file_s_best_node(tmp_path):
    """Diversifying across files must not change *which* node represents a
    file that does get more than one slot -- busy.py's best-ranked symbol,
    not an arbitrary one, when k is large enough to reach a second busy.py
    node too."""
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/busy.py": """
            def alpha(v):
                return v


            def beta(v):
                return v


            def gamma(v):
                return v
            """,
            "pkg/quiet.py": "def delta(v):\n    return v\n",
        },
    )
    cg = build_graph(tmp_path)
    text = "alpha() and beta() and gamma() are all wrong, and so is delta()"

    ctx = GraphRetriever(k=3, graph=cg).retrieve(issue(text), RepoRef(root=tmp_path))
    busy_symbols = {s.symbol for s in ctx.snippets if s.path == "pkg/busy.py"}
    assert "alpha" in busy_symbols, "the file's top-ranked symbol must still win its slot"


# -- snippet fidelity ------------------------------------------------------


def test_every_snippet_text_matches_its_declared_span(cg, repo):
    ctx = GraphRetriever(k=6, graph=cg).retrieve(issue("broken() is wrong"), RepoRef(root=repo))
    assert ctx.snippets
    for s in ctx.snippets:
        lines = (repo / s.path).read_text(encoding="utf-8").splitlines()
        assert s.text.splitlines() == lines[s.start_line - 1 : s.end_line]


def test_an_issue_matching_nothing_yields_an_empty_context(cg, repo):
    ctx = GraphRetriever(graph=cg).retrieve(issue("the quick brown fox"), RepoRef(root=repo))
    assert ctx.snippets == () and ctx.stats["seeds"] == 0


def test_reasons_are_carried_onto_the_snippets(cg, repo):
    ctx = GraphRetriever(k=3, graph=cg).retrieve(issue("broken() is wrong"), RepoRef(root=repo))
    assert ctx.snippets
    assert all(s.reason.startswith("graph rank ") for s in ctx.snippets)


# -- plumbing --------------------------------------------------------------


def test_an_injected_graph_is_reused_rather_than_rebuilt(cg, repo, monkeypatch):
    """The eval sweeps seven configurations per instance; rebuilding each time
    would cost 36s x 7 on django for no new information."""
    import cartographer.retrieval.graph_retriever as mod

    def explode(*a, **k):
        raise AssertionError("build_graph called despite an injected graph")

    monkeypatch.setattr(mod, "build_graph", explode)
    assert GraphRetriever(graph=cg).retrieve(issue("broken() is wrong"), RepoRef(root=repo))


def test_min_confidence_reaches_the_walk(tmp_path):
    """The ablation's whole question rides on this being plumbed end to end.

    The only route to `K.singular_name` is a repo-wide name match (confidence
    0.5), so raising the floor above it must drop the node entirely.
    """
    cg = build_graph(
        write(
            tmp_path,
            {
                "m.py": """
                def entry(obj):
                    obj.singular_name()
                """,
                "other.py": """
                class K:
                    def singular_name(self):
                        pass
                """,
            },
        )
    )
    target = node_id("other.py", "K.singular_name")

    loose, _ = GraphRetriever(min_confidence=0.0, graph=cg).rank(issue("entry() is wrong"), cg)
    strict, stats = GraphRetriever(min_confidence=0.6, graph=cg).rank(
        issue("entry() is wrong"), cg
    )
    assert target in loose, "test is vacuous unless the weak edge is used at all"
    assert target not in strict
    assert stats["min_confidence"] == 0.6
