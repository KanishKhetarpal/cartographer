"""`eval/report.py` has no CI coverage of its own -- it's a rendering script, not
part of the shipped package, so nothing catches a broken section silently
printing an empty table or crashing on a missing file. Its logic (delta math,
None-handling when a results file is absent or a k is missing) is pure and
needs no repo checkout or model, so there's no reason for it to be
manual-only the way the harnesses that produce its input are.
"""

from __future__ import annotations

from eval.report import ablation_section, embedding_section, hitrate_section


def test_hitrate_section_missing_file_says_so() -> None:
    out = hitrate_section(None)
    assert any("No" in line and "phase1_hitrate" in line for line in out)


def test_hitrate_section_renders_delta() -> None:
    d = {
        "summary": {"instances": 1, "recall_graph@1": 0.5, "recall_seeds@1": 0.3},
        "rows": [{"repo": "psf/requests"}, {"repo": "psf/requests", "error": "checkout failed"}],
    }
    out = hitrate_section(d)
    table = "\n".join(out)
    assert "| 1 | 0.500 | 0.300 | +0.200 |" in table
    # the errored row must not count toward the instance total
    assert "requests 1" in table


def test_embedding_section_missing_phase4_says_so() -> None:
    out = embedding_section({"summary": {}}, None)
    assert any("phase4_hitrate" in line for line in out)


def test_embedding_section_missing_phase1_says_so() -> None:
    # graph numbers are required to compute a delta -- without them the
    # section must refuse to render a one-sided table, not silently drop the
    # graph column.
    out = embedding_section(None, {"summary": {}})
    assert any("phase1_hitrate" in line for line in out)


def test_embedding_section_computes_delta_per_k() -> None:
    hitrate = {"summary": {"recall_graph@10": 0.684}}
    embed = {"summary": {"instances": 59, "recall_embedding@10": 0.708}}
    out = embedding_section(hitrate, embed)
    table = "\n".join(out)
    assert "| 10 | 0.684 | 0.708 | -0.024 |" in table


def test_embedding_section_skips_k_missing_from_either_side() -> None:
    # phase4_hitrate.json only ever has embedding@{1,3,5,10,20} -- if a
    # future run were partial, a k present in one summary but not the other
    # must be skipped, not rendered with a crash or a fabricated 0.0.
    hitrate = {"summary": {"recall_graph@1": 0.5}}
    embed = {"summary": {"instances": 1}}  # no embedding@1 at all
    out = embedding_section(hitrate, embed)
    assert "| 1 |" not in "\n".join(out)


def test_ablation_section_missing_file_says_so() -> None:
    out = ablation_section(None, "Small repos")
    assert any("No results" in line and "Small repos" in line for line in out)


def test_ablation_section_includes_noise_floor_caveat() -> None:
    vals = {"1": 0.356, "3": 0.556, "5": 0.616, "10": 0.684, "20": 0.774}
    d = {"n": 59, "recall": {"baseline": vals}, "configs": ["baseline"]}
    out = ablation_section(d, "Small repos")
    table = "\n".join(out)
    assert "1.7 points" in table
    assert "| baseline | 0.356 | 0.556 | 0.616 | 0.684 | 0.774 |" in table
