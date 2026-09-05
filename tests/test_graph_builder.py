"""Graph builder contract.

Every test here corresponds to something that was measured wrong on a real
checkout before it was fixed, or to a rule the ranking depends on. The three
defects the flask run exposed are pinned by name:

  * an external receiver must not fall through to a short-name guess
  * `pkg.Thing` must resolve through `__init__.py` re-exports
  * two files claiming one module name must not pool their symbol tables
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.graph_builder import (
    C_AMBIGUOUS,
    C_ATTRIBUTE,
    C_DIRECT,
    C_SELF_MRO,
    C_UNIQUE_NAME,
    MODULE_SYMBOL,
    build_graph,
    module_name,
    node_id,
    resolve_relative,
)


def write(root, files: dict[str, str]):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).strip() + "\n", encoding="utf-8")
    return root


def calls(cg, src: str):
    """(target, confidence, via) for every call edge out of `src`."""
    return {
        (v, d["confidence"], d["via"])
        for u, v, k, d in cg.g.edges(keys=True, data=True)
        if k == "calls" and u == src
    }


def call_targets(cg, src):
    return {t for t, _, _ in calls(cg, src)}


# -- module naming ---------------------------------------------------------


def test_module_name_handles_flat_src_and_package_layouts(tmp_path):
    write(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/sub/__init__.py": "",
            "src/pkg/sub/mod.py": "",
            "top.py": "",
        },
    )
    assert module_name("src/pkg/__init__.py", tmp_path) == "pkg"
    assert module_name("src/pkg/sub/mod.py", tmp_path) == "pkg.sub.mod"
    assert module_name("top.py", tmp_path) == "top"


@pytest.mark.parametrize(
    ("importer", "is_pkg", "level", "module", "expected"),
    [
        ("pkg.a.b", False, 1, "sib", "pkg.a.sib"),
        ("pkg.a.b", False, 2, "other", "pkg.other"),
        ("pkg.a.b", False, 1, "", "pkg.a"),
        # An __init__.py *is* its package, so level 1 means the package itself.
        # Reversing this misroutes every intra-package import in the repo.
        ("pkg.a", True, 1, "mod", "pkg.a.mod"),
        ("pkg.a", True, 2, "mod", "pkg.mod"),
        ("anything", False, 0, "abs.mod", "abs.mod"),
    ],
)
def test_resolve_relative(importer, is_pkg, level, module, expected):
    assert resolve_relative(importer, is_pkg, level, module) == expected


# -- structure -------------------------------------------------------------


def test_contains_edges_link_module_to_class_to_method(tmp_path):
    cg = build_graph(
        write(tmp_path, {"m.py": "class C:\n    def meth(self):\n        pass\n"})
    )
    mod, cls, meth = node_id("m.py", MODULE_SYMBOL), node_id("m.py", "C"), node_id("m.py", "C.meth")
    contains = {(u, v) for u, v, k in cg.g.edges(keys=True) if k == "contains"}
    assert (mod, cls) in contains and (cls, meth) in contains


def test_import_edges_are_module_to_module_and_only_for_in_repo_targets(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "import os\nfrom pkg.b import helper\n",
                "pkg/b.py": "def helper():\n    pass\n",
            },
        )
    )
    imports = {(u, v) for u, v, k in cg.g.edges(keys=True) if k == "imports"}
    assert (node_id("pkg/a.py", MODULE_SYMBOL), node_id("pkg/b.py", MODULE_SYMBOL)) in imports
    assert cg.stats["import_external"] == 1  # os


def test_node_for_line_returns_the_innermost_symbol(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "m.py": """
                class C:
                    def meth(self):
                        x = 1
                        return x
                """,
            },
        )
    )
    assert cg.node_for_line("m.py", 3) == node_id("m.py", "C.meth")
    assert cg.node_for_line("m.py", 1) == node_id("m.py", "C")


# -- call resolution -------------------------------------------------------


def test_a_bare_name_bound_by_an_import_resolves_exactly(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "from pkg.b import helper\n\n\ndef entry():\n    return helper()\n",
                "pkg/b.py": "def helper():\n    pass\n",
            },
        )
    )
    assert calls(cg, node_id("pkg/a.py", "entry")) == {
        (node_id("pkg/b.py", "helper"), C_DIRECT, "direct")
    }


def test_an_external_receiver_never_falls_back_to_a_short_name_guess(tmp_path):
    """The defect this closes was 415 call sites on flask: `os.path.join` was
    landing on any repo function called `join`."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "m.py": "import os\n\n\ndef f():\n    os.path.join('a')\n",
                "other.py": "def join():\n    pass\n",
            },
        )
    )
    assert calls(cg, node_id("m.py", "f")) == set()
    assert cg.stats["call_external"] == 1
    assert "call_unique_name" not in cg.stats


def test_an_attribute_on_an_in_repo_module_resolves(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "import pkg.b\n\n\ndef f():\n    pkg.b.helper()\n",
                "pkg/b.py": "def helper():\n    pass\n",
            },
        )
    )
    assert calls(cg, node_id("pkg/a.py", "f")) == {
        (node_id("pkg/b.py", "helper"), C_ATTRIBUTE, "attribute")
    }


def test_re_exports_through_init_resolve(tmp_path):
    """`flask.Flask` is defined in app.py and re-exported by __init__.py. Most
    Python libraries are consumed this way; resolving only defined symbols
    leaves a package's whole public API unresolvable inside its own repo."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "from pkg.app import Flask\n",
                "pkg/app.py": "class Flask:\n    pass\n",
                "user.py": "import pkg\n\n\ndef f():\n    pkg.Flask()\n",
            },
        )
    )
    assert calls(cg, node_id("user.py", "f")) == {
        (node_id("pkg/app.py", "Flask"), C_ATTRIBUTE, "attribute")
    }, "must resolve through the re-export, not by a short-name guess"


def test_re_export_chains_resolve_through_two_hops(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "from pkg.mid import Thing\n",
                "pkg/mid.py": "from pkg.deep import Thing\n",
                "pkg/deep.py": "class Thing:\n    pass\n",
                "user.py": "import pkg\n\n\ndef f():\n    pkg.Thing()\n",
            },
        )
    )
    assert calls(cg, node_id("user.py", "f")) == {
        (node_id("pkg/deep.py", "Thing"), C_ATTRIBUTE, "attribute")
    }


def test_self_calls_resolve_through_the_inheritance_chain(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/base.py": "class Base:\n    def helper(self):\n        pass\n",
                "pkg/child.py": """
                from pkg.base import Base


                class Child(Base):
                    def run(self):
                        return self.helper()
                """,
            },
        )
    )
    assert calls(cg, node_id("pkg/child.py", "Child.run")) == {
        (node_id("pkg/base.py", "Base.helper"), C_SELF_MRO, "self_mro")
    }
    inherits = {(u, v) for u, v, k in cg.g.edges(keys=True) if k == "inherits"}
    assert (node_id("pkg/child.py", "Child"), node_id("pkg/base.py", "Base")) in inherits


def test_a_unique_short_name_resolves_at_reduced_confidence(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "m.py": "def f(obj):\n    obj.singular_name()\n",
                "other.py": "class K:\n    def singular_name(self):\n        pass\n",
            },
        )
    )
    assert calls(cg, node_id("m.py", "f")) == {
        (node_id("other.py", "K.singular_name"), C_UNIQUE_NAME, "unique_name")
    }


def test_two_candidates_fan_out_at_low_confidence(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "m.py": "def f(obj):\n    obj.dup()\n",
                "a.py": "def dup():\n    pass\n",
                "b.py": "def dup():\n    pass\n",
            },
        )
    )
    assert calls(cg, node_id("m.py", "f")) == {
        (node_id("a.py", "dup"), C_AMBIGUOUS, "ambiguous"),
        (node_id("b.py", "dup"), C_AMBIGUOUS, "ambiguous"),
    }


def test_beyond_max_ambiguity_no_edge_is_emitted_but_the_drop_is_counted(tmp_path):
    """`get` is called at 385 sites in flask. Fanning that out would spend the
    token budget on files that are wrong nearly every time; a missing edge is
    the lesser harm, and counting it keeps the cost visible."""
    files = {"m.py": "def f(obj):\n    obj.dup()\n"}
    files |= {f"d{i}.py": "def dup():\n    pass\n" for i in range(4)}
    cg = build_graph(write(tmp_path, files))
    assert calls(cg, node_id("m.py", "f")) == set()
    assert cg.stats["call_too_ambiguous"] == 1


def test_max_ambiguity_is_configurable(tmp_path):
    files = {"m.py": "def f(obj):\n    obj.dup()\n"}
    files |= {f"d{i}.py": "def dup():\n    pass\n" for i in range(4)}
    cg = build_graph(write(tmp_path, files), max_ambiguity=4)
    assert len(calls(cg, node_id("m.py", "f"))) == 4


def test_direct_recursion_adds_no_edge(tmp_path):
    cg = build_graph(write(tmp_path, {"m.py": "def f(n):\n    return f(n - 1)\n"}))
    assert calls(cg, node_id("m.py", "f")) == set()


def test_a_call_at_module_scope_is_attributed_to_the_module_node(tmp_path):
    cg = build_graph(
        write(tmp_path, {"m.py": "def setup():\n    pass\n\n\nsetup()\n"})
    )
    assert call_targets(cg, node_id("m.py", MODULE_SYMBOL)) == {node_id("m.py", "setup")}


# -- collisions and robustness --------------------------------------------


def test_two_files_claiming_one_module_name_do_not_pool_their_symbols(tmp_path):
    """Three files in flask are all called `conftest`. Picking the first
    silently attributes one file's symbols to another."""
    cg = build_graph(
        write(
            tmp_path,
            {
                "a/conftest.py": "def only_in_a():\n    pass\n",
                "b/conftest.py": "def only_in_b():\n    pass\n",
                "user.py": "import conftest\n\n\ndef f():\n    conftest.only_in_a()\n",
            },
        )
    )
    assert cg.stats["ambiguous_modules"] == ["conftest"]
    assert "conftest" not in cg.modules
    # It still resolves by unique short name -- but never by claiming to know
    # which `conftest` was meant.
    assert {v for _, v, w in calls(cg, node_id("user.py", "f"))} <= {"unique_name"}


def test_an_unparseable_file_is_skipped_and_the_scan_continues(tmp_path):
    cg = build_graph(
        write(tmp_path, {"bad.py": "def f(:\n    pass\n", "good.py": "def g():\n    pass\n"})
    )
    assert cg.stats["files"] == 1
    assert cg.stats["parse_errors"] == 1
    assert node_id("good.py", "g") in cg.g
    assert node_id("bad.py", "f") not in cg.g


def test_vendored_and_build_directories_are_skipped(tmp_path):
    cg = build_graph(
        write(
            tmp_path,
            {
                "keep.py": "def k():\n    pass\n",
                ".venv/lib/dep.py": "def d():\n    pass\n",
                "build/gen.py": "def gen():\n    pass\n",
                "__pycache__/c.py": "def c():\n    pass\n",
            },
        )
    )
    assert cg.stats["files"] == 1


def test_an_empty_repo_builds_an_empty_graph(tmp_path):
    cg = build_graph(tmp_path)
    assert cg.stats["files"] == 0 and cg.g.number_of_nodes() == 0
