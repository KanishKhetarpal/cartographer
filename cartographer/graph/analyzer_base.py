"""Language analyzer interface.

Deliberate split of responsibility, because getting it wrong makes the second
language a rewrite rather than a plug-in:

  * An analyzer is **syntactic and file-local**. It reports what one file says
    about itself: the symbols it defines, the modules it imports, the names it
    calls, the bases it inherits. It never resolves a name to another file --
    it cannot, since it has not seen the other files.
  * `graph_builder` is **cross-file**. It owns the symbol table and every
    resolution decision (which `foo` did this call mean?).

So a new language costs one `LanguageAnalyzer`, not a new graph builder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


@dataclass(frozen=True, slots=True)
class SymbolDef:
    """A definition site. `qualname` is dotted and file-local, e.g. `Foo.bar`."""

    qualname: str
    kind: SymbolKind
    start_line: int
    end_line: int
    parent: str | None = None
    decorators: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportRef:
    """`import x.y as z` / `from x import y as z`.

    `module` is written exactly as the source writes it, dots and all, so that
    relative imports (`.sibling`) survive to the resolver, which is the only
    layer that knows where this file sits in the package tree.
    """

    module: str
    name: str | None  # None for a plain `import module`
    alias: str | None
    line: int
    level: int = 0  # leading dots on a relative import


@dataclass(frozen=True, slots=True)
class CallRef:
    """A call site. `callee` is the raw dotted expression, unresolved."""

    caller: str | None  # qualname of the enclosing def, None at module scope
    callee: str
    line: int


@dataclass(frozen=True, slots=True)
class BaseRef:
    """`class Child(Base)` -- one edge per base, unresolved."""

    cls: str
    base: str
    line: int


@dataclass(frozen=True, slots=True)
class FileAnalysis:
    """Everything one file says about itself."""

    path: str  # repo-relative, forward slashes
    symbols: tuple[SymbolDef, ...] = ()
    imports: tuple[ImportRef, ...] = ()
    calls: tuple[CallRef, ...] = ()
    bases: tuple[BaseRef, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors


@runtime_checkable
class LanguageAnalyzer(Protocol):
    """One implementation per language. Python first; TypeScript later."""

    language: str
    extensions: tuple[str, ...]

    def analyze(self, path: str, source: str) -> FileAnalysis:
        """Never raises on malformed input -- a syntax error is reported in
        `FileAnalysis.errors` so one bad file cannot abort a whole-repo scan."""
        ...
