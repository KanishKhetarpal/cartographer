"""Module-diagram rendering.

Only the properties a reader depends on are pinned -- clustering, exclusion,
and the refusal to draw something unreadable. The exact mermaid string is
formatting and is deliberately not frozen.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.graph_builder import build_graph
from cartographer.graph.render import module_summary, to_dot, to_mermaid


def write(root, files: dict[str, str]):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).strip() + chr(10), encoding="utf-8")
    return root


@pytest.fixture
def cg(tmp_path):
    return build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/base.py": "def helper():\n    return 1\n",
                "pkg/mid.py": "from pkg.base import helper\n\n\ndef mid():\n    return helper()\n",
                "pkg/top.py": "from pkg.mid import mid\n\n\ndef top():\n    return mid()\n",
                "tests/test_top.py": "from pkg.top import top\n\n\ndef test_top():\n    top()\n",
            },
        )
    )


def test_mermaid_clusters_by_directory_and_draws_import_edges(cg):
    out = to_mermaid(cg)
    assert out.startswith("graph LR")
    assert 'subgraph g0["pkg"]' in out or 'subgraph g1["pkg"]' in out
    assert '"base"' in out and '"mid"' in out
    assert out.count("-->") >= 3


def test_only_import_edges_are_drawn(tmp_path):
    """A call edge is not a module dependency worth drawing; the import that
    made it possible already is."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "a.py": "def f(obj):\n    obj.lonely_name()\n",
                "b.py": "class K:\n    def lonely_name(self):\n        pass\n",
            },
        )
    )
    assert any(k == "calls" for _, _, k in cg.g.edges(keys=True))
    assert "-->" not in to_mermaid(cg)


def test_exclude_drops_matching_paths(cg):
    assert "test_top" in to_mermaid(cg)
    assert "test_top" not in to_mermaid(cg, exclude=("tests",))


def test_too_many_modules_is_refused_not_truncated(tmp_path):
    """A diagram of the 200 busiest files in django is not a diagram, and
    silently drawing a slice of one would misrepresent the repo."""
    files = {"pkg/__init__.py": "", "pkg/hub.py": "def h():\n    return 1\n"}
    for i in range(12):
        files[f"pkg/m{i}.py"] = f"from pkg.hub import h\n\n\ndef f{i}():\n    return h()\n"
    cg = build_graph(write(tmp_path, files))
    with pytest.raises(ValueError, match="exceeds max_nodes"):
        to_mermaid(cg, max_nodes=5)
    assert to_mermaid(cg, max_nodes=100).count("-->") == 12


def test_a_graph_with_no_internal_imports_says_so(tmp_path):
    cg = build_graph(write(tmp_path, {"solo.py": "import os\n\n\ndef f():\n    return os\n"}))
    assert "no internal imports" in to_mermaid(cg)


def test_a_package_init_is_labelled_by_its_package(tmp_path):
    """`__init__` repeated across a diagram names nothing."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "from pkg.mod import thing\n",
                "pkg/mod.py": "thing = 1\n",
                "user.py": "from pkg import thing\n",
            },
        )
    )
    out = to_mermaid(cg)
    assert '"pkg/"' in out and "__init__" not in out


def test_module_summary_ranks_by_fan_in(tmp_path):
    """The chain fixture above cannot show this -- every module in a chain has
    fan-in 1 and the tiebreak decides. This needs a real hub."""
    importer = """
    from pkg.hub import h


    def f{i}():
        return h()
    """
    files = {
        "pkg/__init__.py": "",
        "pkg/hub.py": """
        def h():
            return 1
        """,
        "pkg/loner.py": """
        def unused():
            return 0
        """,
    }
    for i in range(4):
        files[f"pkg/m{i}.py"] = importer.format(i=i)
    cg = build_graph(write(tmp_path, files))

    rows = module_summary(cg)
    by_path = {p: (fin, fout) for p, fin, fout in rows}
    assert rows[0][0] == "pkg/hub.py"
    assert by_path["pkg/hub.py"] == (4, 0), "the hub is imported by all four and imports nothing"
    assert by_path["pkg/m0.py"] == (0, 1)
    assert by_path["pkg/loner.py"] == (0, 0), "a module nobody imports and which imports nobody"


def test_dot_output_is_a_digraph(cg):
    out = to_dot(cg)
    assert out.startswith("digraph modules {") and out.rstrip().endswith("}")
    assert "->" in out


def test_exclude_matches_path_segments_not_bare_substrings(tmp_path):
    """`--exclude eval` once removed the whole `retrieval` package, because
    "retrieval" ends in "eval". A filter that silently drops the most important
    package in the repo is worse than no filter."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/retrieval/__init__.py": "",
                "pkg/retrieval/base.py": "VALUE = 1\n",
                "pkg/retrieval/impl.py": "from pkg.retrieval.base import VALUE\n",
                "eval/harness.py": "from pkg.retrieval.base import VALUE\n",
            },
        )
    )
    out = to_mermaid(cg, exclude=("eval",))
    assert "harness" not in out
    assert "impl" in out, "the retrieval package must survive --exclude eval"
