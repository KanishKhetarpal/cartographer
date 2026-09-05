"""Issue text -> graph seeds.

Seed extraction is also the control the graph is measured against, so its
precision matters twice: a sloppy extractor that scoops up prose would inflate
the baseline and mask whatever the graph contributes.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.graph_builder import build_graph, node_id
from cartographer.retrieval.seeds import extract_seeds, seed_files


def write(root, files: dict[str, str]):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).strip() + chr(10), encoding="utf-8")
    return root


@pytest.fixture
def seedrepo(tmp_path):
    return build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/models.py": """
                class Response:
                    def json(self):
                        return {}

                    def unrelated(self):
                        return None
                """,
                "pkg/other.py": "def helper():\n    pass\n",
            },
        )
    )


def test_a_traceback_frame_seeds_the_symbol_on_that_line(seedrepo):
    text = 'Traceback:\n  File "pkg/models.py", line 3, in json\n    return {}\n'
    seeds = extract_seeds(text, seedrepo)
    top = seeds[0]
    assert top.node == node_id("pkg/models.py", "Response.json")
    assert "traceback" in top.evidence


def test_a_named_file_is_matched_by_path_suffix(seedrepo):
    assert seed_files("see models.py for details", seedrepo) == ["pkg/models.py"]
    assert seed_files("see pkg/models.py", seedrepo) == ["pkg/models.py"]


def test_backticks_promote_a_word_that_prose_alone_would_not_seed(seedrepo):
    """A bare lowercase word is prose until the issue marks it as code."""
    prose = {s.node for s in extract_seeds("the helper is broken", seedrepo)}
    assert node_id("pkg/other.py", "helper") not in prose
    marked = {s.node for s in extract_seeds("the `helper` is broken", seedrepo)}
    assert node_id("pkg/other.py", "helper") in marked


def test_a_backticked_identifier_outweighs_a_bare_mention(seedrepo):
    both = {s.node: s.weight for s in extract_seeds("`helper` and Response", seedrepo)}
    assert both[node_id("pkg/other.py", "helper")] > both[node_id("pkg/models.py", "Response")]


def test_prose_words_do_not_become_seeds(seedrepo):
    """Without a stoplist an issue's ordinary English turns into a query for the
    whole codebase."""
    assert extract_seeds("The result is not the expected value when we run it", seedrepo) == []


def test_a_name_matching_too_many_definitions_is_not_a_seed(tmp_path):
    files = {"m.py": "x = 1\n"}
    files |= {f"d{i}.py": "def dup():\n    pass\n" for i in range(6)}
    cg = build_graph(write(tmp_path, files))
    assert extract_seeds("`dup` is broken", cg) == []


def test_a_call_written_in_prose_is_seeded(seedrepo):
    """`entry() returns one too many` must seed `entry`. This was a real miss:
    a bare lowercase word is prose, but parens make it unambiguously code, and
    without this rule an issue phrased that way seeded nothing at all."""
    seeds = {s.node for s in extract_seeds("json() returns one too many", seedrepo)}
    assert node_id("pkg/models.py", "Response.json") in seeds


def test_the_call_pattern_is_a_word_boundary_not_a_control_character(seedrepo):
    """Regression guard for a stray 0x08 byte that made the pattern unmatchable
    while still compiling and reading correctly in the source."""
    from cartographer.retrieval import seeds as seeds_mod

    backslash = chr(92)
    assert backslash + 'b' in seeds_mod._CALLED.pattern
    assert not any(ord(c) < 32 for c in seeds_mod._CALLED.pattern)
