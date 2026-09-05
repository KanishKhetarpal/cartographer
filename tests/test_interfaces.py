"""Interface tests.

These pin the contract the whole experiment rests on: both retrievers must be
substitutable, and `Context` must be the only thing that crosses the boundary.
They deliberately assert on *shape*, not on retrieval quality -- quality tests
arrive with the real retrievers in Phases 1 and 4.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph import FileAnalysis, LanguageAnalyzer, SymbolDef, SymbolKind
from cartographer.retrieval import (
    MODES,
    Context,
    EmbeddingRetriever,
    GraphRetriever,
    Issue,
    RepoRef,
    Retriever,
    build_retriever,
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(
        textwrap.dedent(
            """
            from pkg.b import helper


            def entry(x):
                return helper(x) + 1
            """
        ).strip(),
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "b.py").write_text("def helper(x):\n    return x * 2\n", encoding="utf-8")
    return RepoRef(root=tmp_path)


@pytest.fixture
def issue():
    return Issue(id="demo-1", title="entry off by one", body="entry() returns one too many.")


@pytest.mark.parametrize("mode", sorted(MODES))
def test_every_mode_satisfies_the_retriever_protocol(mode):
    assert isinstance(build_retriever(mode, k=3), Retriever)


def test_unknown_mode_is_rejected_by_name():
    with pytest.raises(ValueError, match="unknown retrieval mode"):
        build_retriever("telepathy")


@pytest.mark.parametrize("mode", sorted(MODES))
def test_retrievers_return_an_identically_shaped_context(mode, issue, repo):
    ctx = build_retriever(mode, k=3).retrieve(issue, repo)
    assert isinstance(ctx, Context)
    assert ctx.mode == mode
    assert ctx.snippets, "retriever returned nothing for a repo with source in it"
    assert ctx.token_estimate > 0
    for s in ctx.snippets:
        assert s.start_line >= 1
        assert s.end_line >= s.start_line
        assert s.line_count == s.end_line - s.start_line + 1
        # The declared span must match the text actually carried. A snippet that
        # claims a line it does not contain misleads the model and breaks any
        # human cross-check against the file.
        assert s.line_count == len(s.text.splitlines())
        assert not s.path.startswith("/") and "\\" not in s.path


def test_both_modes_agree_on_the_public_surface(issue, repo):
    """The agent loop must not be able to tell the two apart by duck-typing."""
    g = GraphRetriever(k=2).retrieve(issue, repo)
    e = EmbeddingRetriever(k=2).retrieve(issue, repo)
    assert type(g) is type(e)
    def public(o):
        return {n for n in dir(o) if not n.startswith("_")}

    assert public(g) == public(e)
    assert {"mode", "retrieve"} <= public(GraphRetriever(k=2))
    assert {"mode", "retrieve"} <= public(EmbeddingRetriever(k=2))
    assert g.mode != e.mode  # the only Context field that may differ by construction


def test_a_placeholder_retriever_still_flags_itself(issue, repo):
    """Guard against a placeholder run being scored as a result. The graph
    retriever is real as of Phase 1; the embedding baseline lands in Phase 4 and
    must keep saying so until it does."""
    assert EmbeddingRetriever(k=2).retrieve(issue, repo).stats.get("stub") is True
    assert GraphRetriever(k=2).retrieve(issue, repo).stats.get("stub") is None


def test_a_run_is_flagged_stubbed_while_no_model_is_called(issue, repo):
    """The flag the eval harness actually reads. Real retrieval does not make a
    run scoreable -- the loop still emits a placeholder patch."""
    from cartographer.agent.orchestrator import resolve as run_resolve

    assert run_resolve(issue, repo, GraphRetriever(k=2)).stub is True


def test_context_files_are_deduped_in_rank_order(issue, repo):
    ctx = GraphRetriever(k=5).retrieve(issue, repo)
    assert list(ctx.files) == list(dict.fromkeys(s.path for s in ctx.snippets))


def test_repo_ref_coerces_str_to_path(tmp_path):
    assert RepoRef(root=str(tmp_path)).root == tmp_path


def test_issue_text_joins_title_and_body():
    assert Issue(id="i", title="T", body="B").text == "T\n\nB"


def test_a_minimal_analyzer_satisfies_the_language_analyzer_protocol():
    class Toy:
        language = "toy"
        extensions = (".toy",)

        def analyze(self, path: str, source: str) -> FileAnalysis:
            return FileAnalysis(
                path=path,
                symbols=(SymbolDef("f", SymbolKind.FUNCTION, 1, 1),),
            )

    toy = Toy()
    assert isinstance(toy, LanguageAnalyzer)
    out = toy.analyze("x.toy", "")
    assert out.ok and out.symbols[0].kind is SymbolKind.FUNCTION


def test_file_analysis_reports_errors_instead_of_raising():
    assert not FileAnalysis(path="bad.py", errors=("SyntaxError: line 3",)).ok


def test_cli_runs_end_to_end_and_emits_a_patch(repo, tmp_path):
    """The Phase-0 acceptance check: wiring works before any intelligence does."""
    from typer.testing import CliRunner

    from cartographer.cli import app

    issue_file = tmp_path / "issue.txt"
    issue_file.write_text("entry off by one\n\nentry() returns one too many.", encoding="utf-8")
    patch_file = tmp_path / "out.patch"

    res = CliRunner().invoke(
        app,
        ["resolve", "--repo", str(repo.root), "--issue", str(issue_file),
         "--mode", "graph", "--out", str(patch_file)],
    )
    assert res.exit_code == 0, res.output
    assert "STUB RUN" in res.output
    assert patch_file.read_text(encoding="utf-8").startswith("diff --git")


def test_cli_rejects_an_unknown_mode(repo, tmp_path):
    from typer.testing import CliRunner

    from cartographer.cli import app

    issue_file = tmp_path / "issue.txt"
    issue_file.write_text("t\n\nb", encoding="utf-8")
    res = CliRunner().invoke(
        app, ["resolve", "--repo", str(repo.root), "--issue", str(issue_file), "--mode", "nope"]
    )
    assert res.exit_code == 2
