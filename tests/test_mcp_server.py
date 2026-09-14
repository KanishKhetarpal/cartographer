"""cartographer/mcp/server.py's plain functions -- `_resolve_issue` and
`_blast_radius_query` -- not the MCP wiring itself.

Deliberately does not import `mcp.server.mcpserver.MCPServer` or call
`build_server()`: the whole point of the module's lazy-import split (see its
docstring) is that this file, like the module itself, must collect and pass
on the base CI install with no extras. A live MCP session is a manual/local
check, same boundary `test_sandbox.py` draws around the real Docker harness.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.graph_builder import build_graph
from cartographer.mcp.server import (
    _blast_radius_query,
    _resolve_issue,
    blast_radius,
    resolve_issue,
)


def write(root, files: dict[str, str]):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).strip() + chr(10), encoding="utf-8")
    return root


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


# -- resolve_issue -----------------------------------------------------------


def test_resolve_issue_returns_real_context(repo):
    build_graph(repo)  # warm the parse cache the same way retrieve() would
    ctx = _resolve_issue(str(repo), "broken() is wrong", k=3)
    assert ctx["files"], "test is vacuous unless something was actually retrieved"
    assert "pkg/target.py" in ctx["files"]
    assert ctx["mode"] == "graph"
    assert ctx["snippets"]
    snippet = ctx["snippets"][0]
    assert set(snippet) == {"path", "start_line", "end_line", "text", "symbol", "reason"}


def test_resolve_issue_respects_k(repo):
    ctx = _resolve_issue(str(repo), "broken() is wrong, and so is entry()", k=1)
    assert len(ctx["snippets"]) == 1


def test_resolve_issue_rejects_an_unknown_mode(repo):
    with pytest.raises(ValueError, match="unknown mode"):
        _resolve_issue(str(repo), "broken() is wrong", mode="nonexistent")


def test_resolve_issue_never_produces_a_patch_field():
    """The module's whole honesty claim rests on this: resolve_issue must
    never look like it returned a fix, only context. If a `patch` key ever
    appeared here it would be indistinguishable from a real resolution to a
    calling agent that only reads the shape, not the docstring."""
    import inspect

    src = inspect.getsource(_resolve_issue)
    assert "patch" not in src.lower()


def test_resolve_issue_tool_wrapper_matches_the_plain_function(repo):
    """resolve_issue (the tool) must be a thin, behavior-preserving wrapper
    around _resolve_issue, per the module's own docstring claim. Excludes
    stats["build_seconds"]: each call rebuilds the graph independently (no
    injected graph, same as a real MCP call would see), so that one field is
    expected to differ run to run -- it is timing, not behavior."""
    a = resolve_issue(str(repo), "broken() is wrong", k=2)
    b = _resolve_issue(str(repo), "broken() is wrong", k=2)
    a["stats"].pop("build_seconds", None)
    b["stats"].pop("build_seconds", None)
    assert a == b


# -- blast_radius --------------------------------------------------------


def test_blast_radius_reaches_a_caller_the_issue_never_names(repo):
    result = _blast_radius_query(str(repo), "broken() is wrong")
    paths = {r["path"] for r in result["ranked"]}
    assert "pkg/target.py" in paths and "pkg/caller.py" in paths, (
        "test is vacuous unless the walk actually reaches beyond the seed"
    )


def test_blast_radius_reports_seed_provenance(repo):
    result = _blast_radius_query(str(repo), "broken() is wrong")
    assert result["seeds"], "test is vacuous unless a seed was actually extracted"
    seed = result["seeds"][0]
    assert set(seed) == {"node", "path", "weight", "evidence"}
    assert seed["path"] == "pkg/target.py"


def test_blast_radius_min_confidence_reaches_the_call(tmp_path):
    """Confirms min_confidence is actually plumbed through to the real walk,
    not silently dropped. `pkg/target.py`'s `broken()` is called through a
    direct import (confidence 1.0), which a min_confidence filter would never
    touch -- too weak a signal to prove the parameter reaches anything.
    Mirrors test_graph_retriever.py::test_min_confidence_reaches_the_walk's
    fixture instead: the only route to `K.singular_name` is a repo-wide name
    match on an untyped parameter (confidence 0.5), so raising the floor
    above it must drop the node entirely."""
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
    loose = _blast_radius_query(str(tmp_path), "entry() is wrong", min_confidence=0.0)
    strict = _blast_radius_query(str(tmp_path), "entry() is wrong", min_confidence=0.6)
    loose_nodes = {r["node"] for r in loose["ranked"]}
    strict_nodes = {r["node"] for r in strict["ranked"]}
    target = "other.py::K.singular_name"
    assert target in loose_nodes, "test is vacuous unless the weak edge is used at all"
    assert target not in strict_nodes


def test_blast_radius_tool_wrapper_matches_the_plain_function(repo):
    assert blast_radius(str(repo), "broken() is wrong", limit=5) == _blast_radius_query(
        str(repo), "broken() is wrong", limit=5
    )
