"""Cross-file resolution and graph construction.

This is the only layer that has seen more than one file, so every "which `foo`
did that mean?" decision lives here. Analyzers stay file-local and syntactic.

## Why edges carry a confidence

Python call resolution is genuinely undecidable without type inference, and the
honest thing is to say so on the edge rather than to emit confident-looking
edges the graph cannot justify. Measured over flask's 3963 call sites:

    bare name -> import or local def          10.2%   exact
    X.m where X is an imported name           20.9%   exact
    self.m -> resolved through the class MRO   4.0%   near-exact
    repo-wide *unique* short-name match       32.3%   plausible
    ambiguous, 2-3 candidates                 14.0%   weak, fanned out
    ambiguous, 4+ candidates                  14.4%   DROPPED
    no in-repo definition at all              39.2%   correctly no edge

The last two rows are the design point. `get` alone is called at 385 sites in
flask; fanning it out would add hundreds of edges that are wrong nearly every
time, and noise in a blast radius is worse than a missing edge because it
silently spends the token budget on the wrong files. Above `MAX_AMBIGUITY`
candidates we emit nothing and count it, so the loss is visible in `stats`
rather than hidden in the ranking.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

from .analyzer_base import FileAnalysis, LanguageAnalyzer, SymbolKind
from .python_analyzer import PythonAnalyzer

MODULE_SYMBOL = "<module>"

#: Above this many same-named candidates, drop the call rather than fan out.
MAX_AMBIGUITY = 3

#: Confidence per resolution path, ordered by the measurement above.
C_DIRECT = 1.0  # bound by an import, or defined in this very module
C_ATTRIBUTE = 0.9  # X.m where X is a resolved module or class
C_SELF_MRO = 0.85  # self.m found on the class or one of its bases
C_UNIQUE_NAME = 0.5  # exactly one definition repo-wide with that short name
C_AMBIGUOUS = 0.25  # 2..MAX_AMBIGUITY candidates, edge to each

_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "build", "dist", ".eggs", ".tox",
}


def node_id(path: str, qualname: str) -> str:
    return f"{path}::{qualname}"


@dataclass(slots=True)
class CodeGraph:
    """A repo as a directed multigraph of symbols.

    Edge keys are the relation: `contains`, `imports`, `calls`, `inherits`.
    Every `calls` edge carries `confidence` and `via`, so a consumer can filter
    on how the edge was earned instead of trusting all edges equally.
    """

    g: nx.MultiDiGraph
    root: Path
    modules: dict[str, str] = field(default_factory=dict)  # module name -> path
    paths: dict[str, str] = field(default_factory=dict)  # path -> module name
    stats: dict[str, Any] = field(default_factory=dict)

    def symbols_in(self, path: str) -> list[str]:
        return [n for n, d in self.g.nodes(data=True) if d.get("path") == path]

    def node_for_line(self, path: str, line: int) -> str | None:
        """Innermost symbol covering `line` -- how a traceback becomes a seed."""
        best: tuple[int, str] | None = None
        for n, d in self.g.nodes(data=True):
            if d.get("path") != path or d.get("qualname") == MODULE_SYMBOL:
                continue
            if d["start_line"] <= line <= d["end_line"]:
                span = d["end_line"] - d["start_line"]
                if best is None or span < best[0]:
                    best = (span, n)
        return best[1] if best else node_id(path, MODULE_SYMBOL)


# -- module naming ---------------------------------------------------------


def _source_root(path: Path, root: Path) -> Path:
    """Walk up while the directory is a package; the first non-package is the
    import root. Handles both flat and src/ layouts without being told which."""
    cur = path.parent
    while cur != root and (cur / "__init__.py").exists():
        cur = cur.parent
    return cur


def module_name(rel_path: str, root: Path) -> str:
    p = root / rel_path
    src = _source_root(p, root)
    rel = p.relative_to(src)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def resolve_relative(importer: str, is_package: bool, level: int, module: str) -> str:
    """Turn `from ..pkg.mod import x` into an absolute module name.

    A package's `__init__.py` *is* the package, so level 1 from it means the
    package itself rather than its parent -- getting this backwards silently
    misroutes every intra-package import in a repo that uses them.
    """
    if level == 0:
        return module
    base_parts = importer.split(".") if importer else []
    if not is_package:
        base_parts = base_parts[:-1]
    drop = level - 1
    if drop:
        base_parts = base_parts[:-drop] if drop <= len(base_parts) else []
    return ".".join([*base_parts, *(module.split(".") if module else [])])


# -- builder ---------------------------------------------------------------

@dataclass(slots=True)
class _ResolveCtx:
    """Everything resolution needs, bundled so the signatures stay readable."""

    cg: CodeGraph
    bindings: dict[str, dict[str, tuple[str, str]]]
    exports: dict[str, dict[str, str]]
    top_by_path: dict[str, dict[str, str]]
    by_short: dict[str, list[str]]
    owner: dict[str, str]


class GraphBuilder:
    def __init__(
        self,
        analyzers: tuple[LanguageAnalyzer, ...] = (PythonAnalyzer(),),
        *,
        max_ambiguity: int = MAX_AMBIGUITY,
    ) -> None:
        self.analyzers = analyzers
        self.max_ambiguity = max_ambiguity

    # -- discovery ---------------------------------------------------------
    def _analyzer_for(self, path: Path) -> LanguageAnalyzer | None:
        for a in self.analyzers:
            if path.suffix in a.extensions:
                return a
        return None

    def _iter_files(self, root: Path):
        for p in sorted(root.rglob("*")):
            if not p.is_file() or any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            if self._analyzer_for(p):
                yield p

    def build(self, root: Path) -> CodeGraph:
        root = Path(root).resolve()
        t0 = time.perf_counter()
        g = nx.MultiDiGraph()
        cg = CodeGraph(g=g, root=root)

        analyses: dict[str, FileAnalysis] = {}
        errors: list[str] = []

        # Pass 1 -- parse, and register every definition. Nothing is resolved
        # here: resolution needs the whole symbol table to exist first.
        for p in self._iter_files(root):
            rel = p.relative_to(root).as_posix()
            analyzer = self._analyzer_for(p)
            assert analyzer is not None
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # unreadable file must not abort the scan
                errors.append(f"{rel}: {exc}")
                continue
            fa = analyzer.analyze(rel, src)
            if not fa.ok:
                errors.extend(f"{rel}: {e}" for e in fa.errors)
                continue
            analyses[rel] = fa
            mod = module_name(rel, root)
            cg.paths[rel] = mod

            nlines = src.count("\n") + 1
            g.add_node(
                node_id(rel, MODULE_SYMBOL),
                path=rel, qualname=MODULE_SYMBOL, kind=SymbolKind.MODULE,
                start_line=1, end_line=nlines, module=mod, decorators=(),
            )
            for s in fa.symbols:
                g.add_node(
                    node_id(rel, s.qualname),
                    path=rel, qualname=s.qualname, kind=s.kind,
                    start_line=s.start_line, end_line=s.end_line,
                    module=mod, decorators=s.decorators,
                )
                parent = node_id(rel, s.parent) if s.parent else node_id(rel, MODULE_SYMBOL)
                g.add_edge(parent, node_id(rel, s.qualname), key="contains")

        # -- Indexes the resolver reads -----------------------------------
        # Keyed by PATH, not by module name. Three files in flask are all
        # called `conftest`, and pooling their symbol tables under one key
        # merges unrelated definitions and misroutes anything importing it.
        top_by_path: dict[str, dict[str, str]] = defaultdict(dict)
        by_short: dict[str, list[str]] = defaultdict(list)
        claims: dict[str, list[str]] = defaultdict(list)
        for rel, fa in analyses.items():
            claims[cg.paths[rel]].append(rel)
            for s in fa.symbols:
                nid = node_id(rel, s.qualname)
                by_short[s.qualname.rsplit(".", 1)[-1]].append(nid)
                if s.parent is None:
                    top_by_path[rel][s.qualname] = nid

        # A module name claimed by more than one file resolves to nothing. We
        # genuinely cannot tell which `conftest` an importer meant, and picking
        # the first silently attributes one file's symbols to another.
        owner: dict[str, str] = {m: fs[0] for m, fs in claims.items() if len(fs) == 1}
        ambiguous_modules = {m for m, fs in claims.items() if len(fs) > 1}
        cg.modules = dict(owner)

        # -- exports: what a module offers, including re-exports -----------
        # `flask/__init__.py` defines almost nothing and re-exports everything,
        # which is how most Python libraries are consumed. Resolving only
        # *defined* symbols leaves `flask.Flask` unresolvable in its own repo.
        exports: dict[str, dict[str, str]] = {m: dict(top_by_path[f]) for m, f in owner.items()}
        for _ in range(3):  # chains deeper than this are vanishingly rare
            changed = False
            for rel, fa in analyses.items():
                mod = cg.paths[rel]
                if mod not in exports:
                    continue
                is_pkg = rel.endswith("__init__.py")
                for imp in fa.imports:
                    if imp.name is None:
                        continue
                    src_mod = resolve_relative(mod, is_pkg, imp.level, imp.module)
                    hit = exports.get(src_mod, {}).get(imp.name)
                    bound = imp.alias or imp.name
                    if hit and exports[mod].get(bound) != hit:
                        exports[mod][bound] = hit
                        changed = True
            if not changed:
                break

        counts: dict[str, int] = defaultdict(int)

        # -- Pass 2: imports ----------------------------------------------
        # Nearly fully resolvable, and the backbone of the graph: they carry
        # structure even where call resolution gives up.
        # Values are (kind, target) where kind is "symbol", "module" (in-repo)
        # or "external" -- and "external" is load-bearing. Knowing a receiver
        # is NOT in this repo is what stops `os.path.join` falling through to a
        # short-name match on some unrelated repo function called `join`.
        bindings: dict[str, dict[str, tuple[str, str]]] = {}
        for rel, fa in analyses.items():
            mod = cg.paths[rel]
            is_pkg = rel.endswith("__init__.py")
            local: dict[str, tuple[str, str]] = {}
            for imp in fa.imports:
                target_mod = resolve_relative(mod, is_pkg, imp.level, imp.module)
                known = target_mod in owner

                if imp.name is None:  # `import a.b [as c]`
                    kind = "module" if known else "external"
                    local[imp.alias or target_mod] = (kind, target_mod)
                    if not imp.alias:
                        # `import a.b` also binds `a`, and `a.b.f()` must find
                        # it by longest-prefix match.
                        local[target_mod] = (kind, target_mod)
                        local.setdefault(
                            target_mod.split(".")[0],
                            ("module" if target_mod.split(".")[0] in owner else "external",
                             target_mod.split(".")[0]),
                        )
                else:  # `from a.b import c [as d]`
                    bound = imp.alias or imp.name
                    sym = exports.get(target_mod, {}).get(imp.name)
                    if sym is not None:
                        local[bound] = ("symbol", sym)
                    else:
                        sub = f"{target_mod}.{imp.name}".strip(".")
                        if sub in owner:
                            local[bound] = ("module", sub)
                        elif known or target_mod in ambiguous_modules:
                            # The module is ours and does not export this name:
                            # a submodule we failed to see, or a runtime name.
                            local[bound] = ("module", sub)
                        else:
                            local[bound] = ("external", sub)

                if known:
                    counts["import_edges"] += 1
                    g.add_edge(
                        node_id(rel, MODULE_SYMBOL),
                        node_id(owner[target_mod], MODULE_SYMBOL),
                        key="imports", line=imp.line,
                    )
                else:
                    counts["import_external"] += 1
            bindings[rel] = local

        ctx = _ResolveCtx(
            cg=cg, bindings=bindings, exports=exports, top_by_path=top_by_path,
            by_short=by_short, owner=owner,
        )

        # -- Pass 3: inheritance -------------------------------------------
        # Before calls, because self.m resolution walks these edges.
        bases_of: dict[str, list[str]] = defaultdict(list)
        for rel, fa in analyses.items():
            for b in fa.bases:
                src_node = node_id(rel, b.cls)
                target, external = self._resolve_dotted(b.base, rel, ctx)
                if target and target != src_node:
                    bases_of[src_node].append(target)
                    counts["inherit_edges"] += 1
                    g.add_edge(src_node, target, key="inherits", line=b.line)
                elif external:
                    counts["inherit_external"] += 1
                else:
                    counts["inherit_unresolved"] += 1

        # -- Pass 4: calls --------------------------------------------------
        methods_of = self._method_index(g)
        for rel, fa in analyses.items():
            for c in fa.calls:
                caller = node_id(rel, c.caller) if c.caller else node_id(rel, MODULE_SYMBOL)
                if caller not in g:
                    continue
                targets, via = self._resolve_call(c.callee, rel, caller, ctx, bases_of, methods_of)
                counts[f"call_{via}"] += 1
                for target, conf in targets:
                    if target == caller:  # direct recursion adds no reachability
                        continue
                    g.add_edge(caller, target, key="calls", confidence=conf, via=via, line=c.line)

        cg.stats = {
            "files": len(analyses),
            "parse_errors": len(errors),
            "errors": errors[:20],
            "nodes": g.number_of_nodes(),
            "edges": g.number_of_edges(),
            "modules": len(owner),
            "ambiguous_modules": sorted(ambiguous_modules),
            "build_seconds": round(time.perf_counter() - t0, 3),
            **dict(counts),
        }
        return cg

    # -- resolution --------------------------------------------------------
    @staticmethod
    def _method_index(g: nx.MultiDiGraph) -> dict[str, dict[str, str]]:
        idx: dict[str, dict[str, str]] = defaultdict(dict)
        for n, d in g.nodes(data=True):
            if d.get("kind") is SymbolKind.METHOD:
                parent = node_id(d["path"], d["qualname"].rsplit(".", 1)[0])
                idx[parent][d["qualname"].rsplit(".", 1)[-1]] = n
        return idx

    def _resolve_dotted(self, spelled: str, rel: str, ctx: _ResolveCtx) -> tuple[str | None, bool]:
        """Resolve a dotted name to a node.

        Returns `(node, external)`. `external` True means the name was traced to
        something outside this repo and the caller must NOT fall back to a
        short-name guess -- 415 call sites in flask are `os.path.join`,
        `pytest.fixture` and friends, and guessing there produces an edge to a
        repo function that merely shares a last segment.
        """
        local = ctx.bindings.get(rel, {})
        parts = spelled.split(".")

        # Longest-prefix match against this file's bindings, so `pkg.mod.Thing`
        # resolves even when only `pkg.mod` was imported.
        for i in range(len(parts), 0, -1):
            head, rest = ".".join(parts[:i]), parts[i:]
            hit = local.get(head)
            if hit is None:
                continue
            kind, target = hit
            if kind == "external":
                return None, True
            if kind == "symbol":
                if not rest:
                    return target, False
                tpath, tqual = target.split("::", 1)
                cand = node_id(tpath, f"{tqual}.{'.'.join(rest)}")
                return (cand if cand in ctx.cg.g else target), False
            # kind == "module", in this repo
            if rest:
                found = ctx.exports.get(target, {}).get(".".join(rest))
                if found:
                    return found, False
                deeper = f"{target}.{'.'.join(rest)}"
                if deeper in ctx.owner:
                    return node_id(ctx.owner[deeper], MODULE_SYMBOL), False
                return None, False
            if target in ctx.owner:
                return node_id(ctx.owner[target], MODULE_SYMBOL), False
            return None, False

        # Defined in this very file.
        own = ctx.top_by_path.get(rel, {}).get(spelled)
        if own:
            return own, False
        if len(parts) == 1:
            hits = ctx.by_short.get(spelled, [])
            if len(hits) == 1:
                return hits[0], False
        return None, False

    def _resolve_call(
        self, callee: str, rel: str, caller: str, ctx: _ResolveCtx, bases_of, methods_of
    ) -> tuple[list[tuple[str, float]], str]:
        """Returns the edges to add and the path that earned them.

        The `via` label is returned even when nothing resolved, so a dropped
        call is counted rather than vanishing -- `call_too_ambiguous` in stats
        is how the cost of MAX_AMBIGUITY stays visible.
        """
        parts = callee.split(".")
        short = parts[-1]

        # `self.m()` -- walk the class then its bases. This is the one place the
        # inheritance edges pay for themselves.
        if len(parts) >= 2 and parts[0] == "self":
            cls = self._enclosing_class(caller, ctx.cg.g)
            if cls is not None:
                seen: set[str] = set()
                queue = [cls]
                while queue:
                    k = queue.pop(0)
                    if k in seen:
                        continue
                    seen.add(k)
                    hit = methods_of.get(k, {}).get(short)
                    if hit:
                        return [(hit, C_SELF_MRO)], "self_mro"
                    queue.extend(bases_of.get(k, ()))

        direct, external = self._resolve_dotted(callee, rel, ctx)
        if direct:
            bare = len(parts) == 1
            return [(direct, C_DIRECT if bare else C_ATTRIBUTE)], (
                "direct" if bare else "attribute"
            )
        if external:
            return [], "external"

        hits = ctx.by_short.get(short, [])
        if not hits:
            return [], "unresolved"
        if len(hits) == 1:
            return [(hits[0], C_UNIQUE_NAME)], "unique_name"
        if len(hits) <= self.max_ambiguity:
            return [(h, C_AMBIGUOUS) for h in hits], "ambiguous"
        return [], "too_ambiguous"  # worse than a missing edge; counted, not hidden

    @staticmethod
    def _enclosing_class(node: str, g: nx.MultiDiGraph) -> str | None:
        d = g.nodes[node]
        if d.get("kind") is not SymbolKind.METHOD:
            return None
        return node_id(d["path"], d["qualname"].rsplit(".", 1)[0])


def build_graph(root: str | Path, **kwargs) -> CodeGraph:
    return GraphBuilder(**kwargs).build(Path(root))
