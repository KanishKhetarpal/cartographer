"""Unit tests for the embedding baseline: the chunker, and the ranking/budget
logic in `rank()` / `retrieve()` with the embedding MODEL faked out.

`numpy` is a base dependency (small, no CUDA weight) so the real cosine-score
and argsort code gets real CI coverage via `_FakeModel`. `sentence-transformers`
+ `torch` (the `embedding` extra, ~2GB with model weights) stay optional and
untouched here -- no test in this file loads a real model or hits the network.
That boundary is the same one `tests/test_sandbox.py` draws around the real
Docker harness: what a REAL model actually ranks on a real repo is a
manual/local check, verified once per change and recorded in CONTEXT.md, not
re-run on every push.
"""

from __future__ import annotations

import textwrap

from cartographer.retrieval.embedding_retriever import (
    DEFAULT_MODEL,
    Chunk,
    EmbeddingRetriever,
    chunk_repo,
)


def test_chunk_repo_splits_a_file_into_non_overlapping_windows(tmp_path):
    lines = [f"line{i}" for i in range(1, 101)]  # 100 lines
    (tmp_path / "m.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    chunks = chunk_repo(tmp_path, chunk_lines=40)

    assert [c.path for c in chunks] == ["m.py"] * 3
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 40), (41, 80), (81, 100)]
    # No overlap: every source line appears in exactly one chunk.
    covered = sorted(int(x[4:]) for c in chunks for x in c.text.splitlines())
    assert covered == list(range(1, 101))


def test_chunk_repo_skips_junk_directories(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "real.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "ignored.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("z = 3\n", encoding="utf-8")

    chunks = chunk_repo(tmp_path)

    assert [c.path for c in chunks] == ["pkg/real.py"]


def test_chunk_repo_drops_whitespace_only_windows(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n\n\n\n", encoding="utf-8")
    chunks = chunk_repo(tmp_path, chunk_lines=1)
    # 4 lines total; the three blank ones must not become empty chunks that
    # would embed to a meaningless vector and waste a slot in the index.
    assert [c.text for c in chunks] == ["x = 1"]


def test_chunk_repo_on_an_empty_repo_returns_nothing(tmp_path):
    assert chunk_repo(tmp_path) == []


def test_chunk_is_a_plain_frozen_record():
    c = Chunk(path="a.py", start_line=1, end_line=2, text="x\ny")
    assert c.path == "a.py" and c.start_line == 1 and c.end_line == 2


def test_embedding_retriever_constructs_without_the_ml_stack():
    """Constructing the retriever, and building nothing, must not import
    sentence-transformers or numpy -- both are lazy, inside methods, so the
    graph engine can be used on a machine with neither installed. This is the
    guarantee that makes it safe for `cartographer.retrieval` to import
    `EmbeddingRetriever` unconditionally at module load."""
    r = EmbeddingRetriever(k=5, chunk_lines=30, model_name="not-a-real-model")
    assert r.mode == "embedding"
    assert r.k == 5
    assert r.chunk_lines == 30
    assert r.model_name == "not-a-real-model"


def test_default_model_is_a_real_pinned_identifier():
    # Not "some string" -- an org/name pair that resolves on the HF hub, so a
    # typo here would silently degrade every embedding run to a 404 caught
    # deep inside sentence-transformers with a confusing traceback.
    assert DEFAULT_MODEL.count("/") == 1


def test_chunk_repo_reads_utf8_and_tolerates_bad_bytes(tmp_path):
    (tmp_path / "m.py").write_bytes("s = 'é'\n".encode())
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe not valid utf-8 alone\n")
    chunks = chunk_repo(tmp_path)
    paths = {c.path for c in chunks}
    # Both are chunked -- errors="replace" on the bad file, never a crash that
    # would abort indexing the rest of the repo over one bad file.
    assert paths == {"m.py", "bad.py"}


def test_chunk_repo_chunk_lines_is_configurable(tmp_path):
    (tmp_path / "m.py").write_text(
        textwrap.dedent("\n".join(f"x{i} = {i}" for i in range(10))) + "\n", encoding="utf-8"
    )
    assert len(chunk_repo(tmp_path, chunk_lines=10)) == 1
    assert len(chunk_repo(tmp_path, chunk_lines=3)) == 4  # 3,3,3,1


class _FakeModel:
    """A `.encode()` double, in the shape sentence-transformers returns
    (2-D `float` array), so `rank()`'s real ranking math -- the cosine
    product and the argsort -- runs for real without loading torch. What is
    faked is the embedding step only, not the code under test."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def encode(self, texts, **_kwargs):
        import numpy as np

        return np.array([self.vectors[t] for t in texts])


def test_rank_orders_chunks_by_cosine_similarity(tmp_path, monkeypatch):
    import cartographer.retrieval.embedding_retriever as er

    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    chunks = er.chunk_repo(tmp_path, chunk_lines=1)
    assert [c.path for c in chunks] == ["a.py", "b.py"]

    # Unit vectors along two axes: chunk 0 (a.py) is closest to the query,
    # chunk 1 (b.py) is orthogonal to it -- so the true ranking is unambiguous
    # and independent of any real model's opinion.
    fake = _FakeModel(
        {
            chunks[0].text: [1.0, 0.0],
            chunks[1].text: [0.0, 1.0],
            "issue about a": [1.0, 0.0],
        }
    )
    monkeypatch.setattr(er, "_load_model", lambda _name: fake)

    from cartographer.retrieval.base import Issue, RepoRef

    r = er.EmbeddingRetriever(chunk_lines=1)
    ranked_chunks, scores, order = r.rank(
        Issue(id="x", title="", body="issue about a"), RepoRef(root=tmp_path)
    )

    assert [ranked_chunks[i].path for i in order] == ["a.py", "b.py"]
    assert scores[order[0]] > scores[order[1]]


def test_retrieve_respects_k_and_the_token_budget(tmp_path, monkeypatch):
    import cartographer.retrieval.embedding_retriever as er

    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.py").write_text(f"{name} = 1\n", encoding="utf-8")
    chunks = er.chunk_repo(tmp_path, chunk_lines=1)
    fake = _FakeModel(
        {
            chunks[0].text: [3.0, 0.0],
            chunks[1].text: [2.0, 0.0],
            chunks[2].text: [1.0, 0.0],
            "q": [1.0, 0.0],
        }
    )
    monkeypatch.setattr(er, "_load_model", lambda _name: fake)

    from cartographer.retrieval.base import Issue, RepoRef

    issue = Issue(id="x", title="", body="q")
    repo = RepoRef(root=tmp_path)

    ctx = er.EmbeddingRetriever(k=2, chunk_lines=1).retrieve(issue, repo)
    assert len(ctx.snippets) == 2
    assert ctx.mode == "embedding"
    assert [s.path for s in ctx.snippets] == ["a.py", "b.py"]  # rank order preserved

    tiny_budget = len(chunks[0].text) // er.CHARS_PER_TOKEN
    ctx_budgeted = er.EmbeddingRetriever(k=10, chunk_lines=1).retrieve(
        issue, repo, budget_tokens=tiny_budget
    )
    # At least one snippet always ships, even under a budget too small for a
    # second one -- a context of nothing is strictly worse than a context of
    # the single best-ranked chunk (GraphRetriever makes the same promise).
    assert len(ctx_budgeted.snippets) == 1
    assert ctx_budgeted.snippets[0].path == "a.py"


def test_retrieve_skips_an_oversized_chunk_but_keeps_filling(tmp_path, monkeypatch):
    """A chunk that would overshoot the budget is skipped, not fatal -- a
    later, smaller-ranked chunk that still fits is still offered. This is a
    different code path from "budget already exhausted" above: here `used` is
    still under budget when the oversized chunk is considered, so only the
    inner per-chunk check (not the outer loop-exit check) can catch it."""
    import cartographer.retrieval.embedding_retriever as er
    from cartographer.retrieval.base import Issue, RepoRef

    # Ranked order a, b, c with costs designed so b alone would overshoot a
    # budget that has room left after a, but c fits in what remains.
    (tmp_path / "a.py").write_text("aaa\n", encoding="utf-8")       # cost 1
    (tmp_path / "b.py").write_text("b" * 44 + "\n", encoding="utf-8")  # cost 11, too big
    (tmp_path / "c.py").write_text("cc\n", encoding="utf-8")        # cost 0-1
    chunks = er.chunk_repo(tmp_path, chunk_lines=1)
    fake = _FakeModel(
        {
            chunks[0].text: [3.0, 0.0],
            chunks[1].text: [2.0, 0.0],
            chunks[2].text: [1.0, 0.0],
            "q": [1.0, 0.0],
        }
    )
    monkeypatch.setattr(er, "_load_model", lambda _name: fake)

    cost_a = len(chunks[0].text) // er.CHARS_PER_TOKEN
    cost_c = len(chunks[2].text) // er.CHARS_PER_TOKEN
    budget = cost_a + cost_c + 1
    ctx = er.EmbeddingRetriever(k=10, chunk_lines=1).retrieve(
        Issue(id="x", title="", body="q"), RepoRef(root=tmp_path), budget_tokens=budget
    )
    assert [s.path for s in ctx.snippets] == ["a.py", "c.py"]  # b skipped, c still reached
