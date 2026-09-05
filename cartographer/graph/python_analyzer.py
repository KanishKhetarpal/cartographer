"""Python analyzer built on the stdlib `ast`.

Sits behind `LanguageAnalyzer`, so tree-sitter can replace it without touching
the graph builder. `ast` is used rather than tree-sitter for v0 because it is
exact for the language we care most about, has no build step, and gives
`end_lineno` for free -- and snippet spans are load-bearing.

**This layer resolves nothing.** A call is recorded as the dotted expression the
source wrote (`self.foo`, `json.dumps`, `resp.headers.add`); deciding what those
name is the graph builder's job, because it is the only layer that has seen more
than one file. See `analyzer_base` for why that split matters.

Measured on flask (83 files, 1622 symbols, 3963 calls): every call site falls
into one of a handful of receiver shapes, and only ~10% are a bare name bound by
an import or a local def. The rest is what the builder's confidence tiers exist
to handle honestly.
"""

from __future__ import annotations

import ast

from .analyzer_base import BaseRef, CallRef, FileAnalysis, ImportRef, SymbolDef, SymbolKind

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def decorator_name(node: ast.expr) -> str | None:
    """Name of a decorator, whether or not it is applied as a call.

    `@app.cached` is an Attribute but `@app.route("/x")` is a Call wrapping one,
    and the call form is the common one (measured on flask: 419 call-form vs 135
    bare). Dotting the Call node directly returns None and silently loses every
    `@app.route` -- the most informative decorator there is.
    """
    return dotted(node.func if isinstance(node, ast.Call) else node)


def dotted(node: ast.expr) -> str | None:
    """Flatten an attribute/name expression to its source spelling.

    `json.dumps` -> "json.dumps"; `self.app.config` -> "self.app.config".
    Anything with a subscript, call or literal in the chain returns None: those
    receivers are values rather than names, so no amount of name matching can
    honestly resolve them and pretending otherwise manufactures edges.
    """
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.symbols: list[SymbolDef] = []
        self.imports: list[ImportRef] = []
        self.calls: list[CallRef] = []
        self.bases: list[BaseRef] = []
        # Scope of enclosing *definitions*, innermost last. Drives qualnames and
        # tells a method from a function.
        self._scope: list[tuple[str, SymbolKind]] = []

    # -- helpers ---------------------------------------------------------
    @property
    def _qual_prefix(self) -> str:
        return ".".join(name for name, _ in self._scope)

    @property
    def _enclosing_callable(self) -> str | None:
        """Qualname of the nearest enclosing function, or None at module or
        class body scope. A call in a class body is not made *by* a method."""
        for i in range(len(self._scope) - 1, -1, -1):
            if self._scope[i][1] in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                return ".".join(name for name, _ in self._scope[: i + 1])
        return None

    def _span(self, node: ast.AST) -> tuple[int, int]:
        """Line span including decorators.

        A decorator is part of what the symbol *is* -- `@app.route("/x")` is
        often the most informative line in a Flask view -- and a snippet that
        starts at `def` silently drops it.
        """
        start = node.lineno  # type: ignore[attr-defined]
        for dec in getattr(node, "decorator_list", ()):
            start = min(start, dec.lineno)
        return start, getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]

    # -- definitions -----------------------------------------------------
    def _visit_def(self, node: ast.AST, kind: SymbolKind) -> None:
        name: str = node.name  # type: ignore[attr-defined]
        parent = self._qual_prefix or None
        qualname = f"{parent}.{name}" if parent else name
        start, end = self._span(node)

        self.symbols.append(
            SymbolDef(
                qualname=qualname,
                kind=kind,
                start_line=start,
                end_line=end,
                parent=parent,
                decorators=tuple(
                    d
                    for d in (decorator_name(x) for x in getattr(node, "decorator_list", ()))
                    if d
                ),
            )
        )
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                spelled = dotted(base)
                if spelled:
                    self.bases.append(BaseRef(cls=qualname, base=spelled, line=node.lineno))

        # Decorators are evaluated in the *enclosing* scope, so they are visited
        # before the scope is pushed -- otherwise `@foo` on a method is recorded
        # as a call made by the method it decorates.
        for dec in getattr(node, "decorator_list", ()):
            self.visit(dec)

        self._scope.append((name, kind))
        for child in node.body:  # body only: decorators are already done
            self.visit(child)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        inside_class = bool(self._scope) and self._scope[-1][1] is SymbolKind.CLASS
        self._visit_def(node, SymbolKind.METHOD if inside_class else SymbolKind.FUNCTION)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_def(node, SymbolKind.CLASS)

    # -- imports ---------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ImportRef(module=alias.name, name=None, alias=alias.asname, line=node.lineno)
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # `module` is None for `from . import x`; the dots live in `level` and
        # only the resolver knows where this file sits in the package tree.
        for alias in node.names:
            self.imports.append(
                ImportRef(
                    module=node.module or "",
                    name=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                    level=node.level or 0,
                )
            )

    # -- calls -----------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        spelled = dotted(node.func)
        if spelled:
            self.calls.append(
                CallRef(caller=self._enclosing_callable, callee=spelled, line=node.lineno)
            )
        self.generic_visit(node)


class PythonAnalyzer:
    language = "python"
    extensions = (".py", ".pyi")

    def analyze(self, path: str, source: str) -> FileAnalysis:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError) as exc:
            # Never raise: one unparseable file must not abort a repo scan. Real
            # checkouts contain Python 2 files, templates and test fixtures that
            # are deliberately invalid.
            return FileAnalysis(path=path, errors=(f"{type(exc).__name__}: {exc}",))
        except RecursionError:
            return FileAnalysis(path=path, errors=("RecursionError: expression too deeply nested",))

        c = _Collector(path)
        for stmt in tree.body:
            c.visit(stmt)
        return FileAnalysis(
            path=path,
            symbols=tuple(c.symbols),
            imports=tuple(c.imports),
            calls=tuple(c.calls),
            bases=tuple(c.bases),
        )
