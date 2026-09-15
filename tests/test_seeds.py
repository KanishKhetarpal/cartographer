"""Issue text -> graph seeds.

Seed extraction is also the control the graph is measured against, so its
precision matters twice: a sloppy extractor that scoops up prose would inflate
the baseline and mask whatever the graph contributes.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.graph_builder import MODULE_SYMBOL, build_graph, node_id
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


def test_a_backticked_dotted_name_also_weakly_seeds_a_same_named_sibling(tmp_path):
    """`Response.json` resolves unambiguously via its full qualname -- but the
    code-span extractor also bumps the bare short name ("json") as its own,
    separately-weighted (1.6) candidate. That candidate falls back to
    by_short, which returns *every* symbol named `json` anywhere, not just
    the one the full name already resolved. So a second, unrelated
    `Other.json` gets a weak nomination too (share of 1.6 / 2 hits = 0.8): a
    hedge for when the issue's author meant a different class with the same
    method name.

    The exact weight matters for this test to mean anything: a *generic*
    fallback already exists too (any dotted identifier anywhere in the text
    is also picked up by the plain-identifier loop and bumped at 1.0, short
    name at 0.8/2=0.4) -- so "the sibling gets seeded at all" is true even
    with the code-span-specific bump deleted, and a first draft of this test
    asserted exactly that and passed against the mutated code. Pinning 0.8
    (not just "> 0" or "less than primary") is what actually distinguishes
    the code-span-strength evidence this test targets from the generic one.
    """
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/models.py": "class Response:\n    def json(self):\n        return {}\n",
                "pkg/other.py": "class Other:\n    def json(self):\n        return {}\n",
            },
        )
    )
    weights = {s.node: s.weight for s in extract_seeds("`Response.json` is wrong", cg)}
    primary = node_id("pkg/models.py", "Response.json")
    sibling = node_id("pkg/other.py", "Other.json")
    assert weights[primary] == 2.0
    assert weights[sibling] == 0.8


def test_a_dotted_call_also_weakly_seeds_a_same_named_sibling(tmp_path):
    """Same fan-out as the code-span test above, through the `_CALLED`
    (parenthesised, un-backticked) pattern instead -- a separate branch in
    _identifiers(), also unguarded before this test, same reason the exact
    weight (1.3 / 2 hits = 0.65, not the generic loop's 0.8/2=0.4) is the
    part that actually distinguishes this from the generic fallback."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/models.py": "class Response:\n    def json(self):\n        return {}\n",
                "pkg/other.py": "class Other:\n    def json(self):\n        return {}\n",
            },
        )
    )
    weights = {s.node: s.weight for s in extract_seeds("Response.json() is wrong", cg)}
    primary = node_id("pkg/models.py", "Response.json")
    sibling = node_id("pkg/other.py", "Other.json")
    assert weights[primary] == 1.5
    assert weights[sibling] == 0.65


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


def test_a_github_permalink_seeds_the_file_and_the_line(seedrepo):
    """Maintainer bug reports link blob permalinks constantly. The URL prefix
    used to defeat the suffix match entirely, so an issue naming the exact line
    of the exact gold file scored zero."""
    url = "https://github.com/org/proj/blob/ce7cccf9645/pkg/models.py#L3"
    seeds = extract_seeds(f"This is the line where it breaks.\n{url}\n", seedrepo)
    assert seeds
    assert seeds[0].node == node_id("pkg/models.py", "Response.json")
    assert "permalink" in seeds[0].evidence


def test_a_permalink_without_a_line_still_seeds_the_file(seedrepo):
    url = "https://github.com/org/proj/blob/abc123/pkg/other.py"
    assert seed_files(url, seedrepo) == ["pkg/other.py"]


def test_a_permalink_without_a_line_seeds_the_module_node_via_extract_seeds(seedrepo):
    """The test above only proves seed_files() (the generic path-mention
    pathway, `_PATH` matching the URL's trailing segment) finds the file.
    extract_seeds() has its own, separate _BLOB_URL branch for a permalink
    with no line number, which offers the *module* node with its own
    "permalink" evidence and 1.6 weight -- distinct code, and unguarded: it
    was still passing the full suite with that branch's body deleted
    entirely. This calls extract_seeds() directly so a regression there
    can't hide behind seed_files() happening to work for a different
    reason."""
    url = "https://github.com/org/proj/blob/abc123/pkg/other.py"
    seeds = extract_seeds(url, seedrepo)
    assert seeds, "test is vacuous unless the permalink actually seeds something"
    matches = [s for s in seeds if "permalink" in s.evidence]
    assert matches, f"no permalink-evidenced seed among {[s.evidence for s in seeds]}"
    assert matches[0].node == node_id("pkg/other.py", MODULE_SYMBOL)
    assert matches[0].weight == 1.6


def test_a_url_prefixed_path_is_matched_by_trimming_leading_segments(seedrepo):
    from cartographer.retrieval.seeds import match_path

    known = list(seedrepo.paths)
    assert match_path("github.com/org/proj/blob/abc/pkg/models.py", known) == ["pkg/models.py"]
    assert match_path("pkg/models.py", known) == ["pkg/models.py"]


def test_a_path_matching_nothing_at_any_trim_level_is_not_a_seed(seedrepo):
    """Distinct from the ambiguous case below: this path never matches *any*
    known file at *any* prefix-trim level, not even multiple candidates --
    match_path's final fallthrough. Unreached before this test: mutating that
    return into a non-empty sentinel still passed the whole suite."""
    from cartographer.retrieval.seeds import match_path

    known = list(seedrepo.paths)
    assert match_path("nowhere/nothing.py", known) == []


def test_an_ambiguous_bare_basename_nominates_nothing(tmp_path):
    """Django has dozens of models.py. Picking an arbitrary one is worse than
    picking none, so a single-segment match must be unique to count."""
    from cartographer.retrieval.seeds import match_path

    cg = build_graph(
        write(tmp_path, {"a/models.py": "x = 1\n", "b/models.py": "y = 2\n"})
    )
    known = list(cg.paths)
    assert match_path("models.py", known) == []
    assert match_path("a/models.py", known) == ["a/models.py"]
