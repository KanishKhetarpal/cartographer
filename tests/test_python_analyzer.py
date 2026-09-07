"""Python analyzer contract.

Two things these guard that measurement alone cannot: the flask fixture used to
design this analyzer happens to use one-name-per-import throughout (675
statements, 675 aliases -- verified, not assumed), so multi-alias imports are
untested by any real-repo run; and decorator attribution is invisible in
aggregate counts but changes which symbol a call is credited to.
"""

from __future__ import annotations

import textwrap

import pytest

from cartographer.graph.analyzer_base import SymbolKind
from cartographer.graph.python_analyzer import PythonAnalyzer, dotted


def analyze(src: str):
    return PythonAnalyzer().analyze("m.py", textwrap.dedent(src).strip() + "\n")


def sym(fa, qualname):
    matches = [s for s in fa.symbols if s.qualname == qualname]
    assert matches, f"{qualname!r} not found in {[s.qualname for s in fa.symbols]}"
    return matches[0]


# -- symbols ---------------------------------------------------------------


def test_nesting_produces_dotted_qualnames_and_the_right_kinds():
    fa = analyze(
        """
        def top():
            def inner():
                pass

        class C:
            def meth(self):
                def helper():
                    pass
        """
    )
    assert sym(fa, "top").kind is SymbolKind.FUNCTION
    assert sym(fa, "top.inner").kind is SymbolKind.FUNCTION
    assert sym(fa, "C").kind is SymbolKind.CLASS
    assert sym(fa, "C.meth").kind is SymbolKind.METHOD
    # Nested inside a method, not directly in the class body -- a function.
    assert sym(fa, "C.meth.helper").kind is SymbolKind.FUNCTION
    assert sym(fa, "C.meth").parent == "C"
    assert sym(fa, "top").parent is None


def test_async_defs_are_collected_like_functions():
    fa = analyze(
        """
        class C:
            async def fetch(self):
                pass
        """
    )
    assert sym(fa, "C.fetch").kind is SymbolKind.METHOD


def test_symbol_span_includes_decorators():
    """A snippet starting at `def` drops the most informative line in a view."""
    fa = analyze(
        """
        import app


        @app.route("/x")
        @app.cached
        def view():
            return 1
        """
    )
    s = sym(fa, "view")
    assert s.start_line == 4, "span must start at the first decorator, not at `def`"
    assert s.end_line == 7
    assert s.decorators == ("app.route", "app.cached")


def test_a_decorator_call_is_credited_to_the_enclosing_scope_not_the_decorated_symbol():
    """`@app.route(...)` is evaluated before `view` exists; crediting it to
    `view` would make the decorated symbol look like a caller of its own
    decorator and pull the wrong node into a blast radius."""
    fa = analyze(
        """
        import app


        @app.route("/x")
        def view():
            helper()
        """
    )
    by_callee = {c.callee: c.caller for c in fa.calls}
    assert by_callee["app.route"] is None
    assert by_callee["helper"] == "view"


def test_a_method_decorator_call_is_not_credited_to_the_decorated_method():
    """Same rule one level down: the decorator runs in the class body, where no
    method is executing, so its caller is None rather than `C.deco_target`."""
    fa = analyze(
        """
        import reg


        class C:
            @reg.register("x")
            def target(self):
                self.helper()
        """
    )
    assert {c.callee: c.caller for c in fa.calls} == {
        "reg.register": None,
        "self.helper": "C.target",
    }


# -- imports ---------------------------------------------------------------


def test_one_import_ref_per_bound_name():
    fa = analyze("from pkg.mod import alpha, beta as b\n")
    assert len(fa.imports) == 2
    alpha, beta = fa.imports
    assert (alpha.module, alpha.name, alpha.alias, alpha.level) == ("pkg.mod", "alpha", None, 0)
    assert (beta.module, beta.name, beta.alias, beta.level) == ("pkg.mod", "beta", "b", 0)


def test_plain_import_records_no_name():
    fa = analyze("import os.path as osp\nimport json\n")
    assert [(i.module, i.name, i.alias) for i in fa.imports] == [
        ("os.path", None, "osp"),
        ("json", None, None),
    ]


@pytest.mark.parametrize(
    ("src", "module", "name", "level"),
    [
        ("from . import sibling", "", "sibling", 1),
        ("from .mod import thing", "mod", "thing", 1),
        ("from ..pkg.mod import thing", "pkg.mod", "thing", 2),
    ],
)
def test_relative_imports_keep_their_level_for_the_resolver(src, module, name, level):
    """Only the resolver knows where this file sits in the package tree, so the
    dots must survive this layer intact."""
    (i,) = analyze(src).imports
    assert (i.module, i.name, i.level) == (module, name, level)


def test_imports_inside_a_function_body_are_still_collected():
    fa = analyze(
        """
        def f():
            import heavy
            return heavy
        """
    )
    assert [i.module for i in fa.imports] == ["heavy"]


def test_imports_under_a_type_checking_guard_are_collected():
    fa = analyze(
        """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from pkg.mod import Thing
        """
    )
    assert "pkg.mod" in {i.module for i in fa.imports}


# -- calls and bases -------------------------------------------------------


def test_call_callee_is_the_source_spelling_unresolved():
    fa = analyze(
        """
        def f(resp):
            json.dumps(1)
            resp.headers.add("a")
            self.helper()
        """
    )
    assert [c.callee for c in fa.calls] == ["json.dumps", "resp.headers.add", "self.helper"]
    assert {c.caller for c in fa.calls} == {"f"}


def test_calls_at_module_scope_have_no_caller():
    fa = analyze("configure()\n")
    assert fa.calls[0].caller is None


def test_a_call_in_a_class_body_is_not_credited_to_a_method():
    """A class body is executed at definition time; no method made that call."""
    fa = analyze(
        """
        class C:
            registry = build()

            def m(self):
                pass
        """
    )
    assert [(c.callee, c.caller) for c in fa.calls] == [("build", None)]


def test_bases_are_recorded_per_base_and_left_unresolved():
    fa = analyze(
        """
        class C(Base, pkg.mod.Mixin):
            pass
        """
    )
    assert [(b.cls, b.base) for b in fa.bases] == [("C", "Base"), ("C", "pkg.mod.Mixin")]


def test_a_base_that_is_not_a_dotted_name_is_dropped_rather_than_guessed():
    fa = analyze("class C(mixins[0]):\n    pass\n")
    assert fa.bases == ()


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("a", "a"),
        ("a.b.c", "a.b.c"),
        ("self.x.y", "self.x.y"),
        ("a[0].b", None),
        ("f().b", None),
        ('"lit".join', None),
    ],
)
def test_dotted_refuses_receivers_that_are_values_rather_than_names(expr, expected):
    """No amount of name matching resolves a subscript or a call result, so
    returning a best guess here would manufacture edges."""
    import ast

    assert dotted(ast.parse(expr, mode="eval").body) == expected


def test_a_nested_call_receiver_is_still_recorded_for_the_outer_call():
    fa = analyze("outer(inner())\n")
    assert sorted(c.callee for c in fa.calls) == ["inner", "outer"]


# -- robustness ------------------------------------------------------------


def test_a_syntax_error_is_reported_not_raised():
    fa = PythonAnalyzer().analyze("broken.py", "def f(:\n    pass\n")
    assert not fa.ok
    assert fa.symbols == () and fa.calls == ()
    assert "SyntaxError" in fa.errors[0]


def test_a_null_byte_is_reported_not_raised():
    fa = PythonAnalyzer().analyze("bad.py", "x = 1\x00\n")
    assert not fa.ok


def test_an_empty_file_analyzes_clean():
    fa = PythonAnalyzer().analyze("empty.py", "")
    assert fa.ok and fa.symbols == ()


def test_an_ast_too_deep_to_walk_is_reported_not_raised():
    """`ast.parse` handles expressions a NodeVisitor cannot, so the walk needs
    its own guard. Found on sympy, whose resolvent_lookup.py is 40KB of nested
    polynomial literals -- without this, one file aborts the whole repo scan,
    which is precisely what this class promises never to do."""
    src = "x = " + "+".join(["1"] * 500) + "\n"
    import ast

    ast.parse(src)  # the parser is fine with it; the visitor is not
    fa = PythonAnalyzer().analyze("deep.py", src)
    assert not fa.ok
    assert "RecursionError" in fa.errors[0] and "walk" not in fa.errors[0]
    assert fa.symbols == ()


def test_an_expression_too_deep_to_parse_is_reported_separately():
    src = "x = " + "+".join(["1"] * 4000) + "\n"
    fa = PythonAnalyzer().analyze("deeper.py", src)
    assert not fa.ok and "RecursionError" in fa.errors[0]
