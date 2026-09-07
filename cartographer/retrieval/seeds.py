"""Issue text -> graph seeds.

This is a *lexical* pass and nothing more: it finds the names an issue actually
writes down and maps them to nodes. It deliberately does no ranking and no
expansion, because it is also the control the graph is measured against. If
seed extraction quietly did half the retriever's job, a graph-vs-baseline number
would be measuring the extractor.

Which is why `seed_files()` exists separately: "the files the issue names" is
the honest floor, and the graph has to beat it to have earned anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..graph.graph_builder import MODULE_SYMBOL, CodeGraph, node_id

#: `File "src/pkg/mod.py", line 42, in handler` -- a traceback frame is the
#: strongest seed there is: it names a file, a line and a function.
_TRACEBACK = re.compile(r'File "([^"]+\.py)", line (\d+)(?:, in (\w+))?')

#: A path-looking token. Requires a separator or a .py suffix so that ordinary
#: prose does not read as a path.
_PATH = re.compile(r"\b((?:[\w.-]+/)*[\w.-]+\.py)\b")

#: A GitHub blob permalink, which names a file and often the exact line:
#: `https://github.com/org/repo/blob/<sha>/pkg/mod.py#L99`. Bug reports written
#: by maintainers link these constantly, and the line they point at is usually
#: the line they think is wrong -- nearly as strong a signal as a traceback.
_BLOB_URL = re.compile(r"https?://\S*?/blob/[^\s/]+/(\S+?\.py)(?:#L(\d+))?")

#: Identifiers worth seeding on. Bare lowercase words are excluded by the
#: stoplist below rather than by the pattern, so `save`/`load` still qualify.
_IDENT = re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\b")

#: `name(` -- a call written out in prose.
_CALLED = re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*\(")

_CODE_SPAN = re.compile(r"`([^`\n]{1,120})`|```[\w]*\n(.*?)```", re.DOTALL)

#: Words that appear in prose and also name functions somewhere in every large
#: repo. Seeding on these turns an issue into a query for the whole codebase.
_STOP = {
    "the", "a", "an", "is", "it", "in", "of", "to", "and", "or", "not", "if", "for", "with",
    "this", "that", "when", "then", "should", "would", "could", "but", "as", "on", "at", "by",
    "from", "we", "i", "you", "be", "have", "has", "was", "were", "are", "do", "does", "did",
    "there", "here", "so", "no", "yes", "can", "will", "may", "get", "set", "add", "run",
    "test", "tests", "code", "bug", "issue", "error", "expected", "actual", "result", "value",
    "values", "type", "types", "true", "false", "none", "self", "return", "returns", "print",
    "python", "version", "line", "lines", "file", "files", "example", "output", "input",
    "使用", "def", "class", "import", "raise", "except", "try", "else", "elif", "while",
}

_MIN_IDENT_LEN = 3

#: A name matching more definitions than this is not a seed -- it is a query for
#: everything. Same reasoning as the builder's MAX_AMBIGUITY, one layer up.
MAX_SEED_MATCHES = 4


@dataclass(frozen=True, slots=True)
class Seed:
    node: str
    weight: float
    evidence: str  # what in the issue produced it


def _identifiers(text: str) -> dict[str, float]:
    """Candidate names with a weight reflecting how the issue mentioned them.

    Weights are ordinal, not tuned: a traceback frame beats a code span beats a
    bare mention. Tuning them against the eval set would be fitting to the
    benchmark.
    """
    found: dict[str, float] = {}

    def bump(name: str, w: float) -> None:
        if len(name) >= _MIN_IDENT_LEN and name.lower() not in _STOP:
            found[name] = max(found.get(name, 0.0), w)

    for m in _TRACEBACK.finditer(text):
        if m.group(3):
            bump(m.group(3), 3.0)

    for m in _CODE_SPAN.finditer(text):
        span = m.group(1) or m.group(2) or ""
        for ident in _IDENT.findall(span):
            bump(ident, 2.0)
            if "." in ident:
                bump(ident.rsplit(".", 1)[-1], 1.6)

    # `entry()` in prose is unambiguously a call, even lowercase and
    # un-backticked. Without this, "entry() returns one too many" seeds nothing.
    for m in _CALLED.finditer(text):
        bump(m.group(1), 1.5)
        if "." in m.group(1):
            bump(m.group(1).rsplit(".", 1)[-1], 1.3)

    for ident in _IDENT.findall(text):
        # A dotted or non-lowercase name in prose is almost certainly code;
        # a bare lowercase word usually is not.
        if "." in ident or not ident.islower() or "_" in ident:
            bump(ident, 1.0)
            if "." in ident:
                bump(ident.rsplit(".", 1)[-1], 0.8)
    return found


def extract_paths(text: str) -> list[str]:
    """Repo-relative-looking paths the issue mentions, longest first so that a
    more specific path wins the suffix match below."""
    seen: dict[str, None] = {}
    for m in _TRACEBACK.finditer(text):
        seen.setdefault(m.group(1), None)
    for m in _PATH.finditer(text):
        seen.setdefault(m.group(1), None)
    return sorted(seen, key=len, reverse=True)


def match_path(mentioned: str, known: list[str]) -> list[str]:
    """Repo paths a mentioned path refers to, longest suffix first.

    Leading segments are trimmed one at a time because a mentioned path is
    frequently prefixed with something the repo does not contain -- a GitHub
    blob URL carries `github.com/org/repo/blob/<sha>/` in front of the part that
    matters, and a plain suffix comparison finds nothing at all. That was
    costing real instances: an issue linking the exact line of the exact gold
    file scored zero.

    A bare basename (`base.py`) is only accepted when it is unambiguous. Django
    has dozens of `models.py`, and nominating an arbitrary one is worse than
    nominating none.
    """
    parts = [p for p in mentioned.strip("./").split("/") if p]
    for i in range(len(parts)):
        cand = "/".join(parts[i:])
        hits = [p for p in known if p == cand or p.endswith("/" + cand)]
        if not hits:
            continue
        if len(parts) - i == 1 and len(hits) > 1:
            return []
        return hits
    return []


def seed_files(text: str, cg: CodeGraph) -> list[str]:
    """The files the issue names, matched by path suffix."""
    known = list(cg.paths)
    out: dict[str, None] = {}
    for mentioned in extract_paths(text):
        for path in match_path(mentioned, known):
            out.setdefault(path, None)
    return list(out)


def extract_seeds(text: str, cg: CodeGraph, *, max_seeds: int = 40) -> list[Seed]:
    """Map an issue to graph nodes, with a weight per node."""
    g = cg.g
    by_qual: dict[str, list[str]] = {}
    by_short: dict[str, list[str]] = {}
    for n, d in g.nodes(data=True):
        q = d.get("qualname")
        if not q or q == MODULE_SYMBOL:
            continue
        by_qual.setdefault(q, []).append(n)
        by_short.setdefault(q.rsplit(".", 1)[-1], []).append(n)

    seeds: dict[str, Seed] = {}

    def offer(node: str, weight: float, evidence: str) -> None:
        prev = seeds.get(node)
        if prev is None or weight > prev.weight:
            seeds[node] = Seed(node=node, weight=weight, evidence=evidence)

    known = list(cg.paths)

    # 1. Traceback frames: file + line -> the innermost symbol on that line.
    for m in _TRACEBACK.finditer(text):
        mentioned, line = m.group(1), int(m.group(2))
        for path in match_path(mentioned, known):
            node = cg.node_for_line(path, line)
            if node:
                offer(node, 4.0, f"traceback {mentioned}:{line}")

    # 1b. A linked blob permalink is the maintainer pointing at the line they
    #     believe is wrong. Weighted just under a traceback frame.
    for m in _BLOB_URL.finditer(text):
        mentioned, lineno = m.group(1), m.group(2)
        for path in match_path(mentioned, known):
            if lineno:
                node = cg.node_for_line(path, int(lineno))
                if node:
                    offer(node, 3.5, f"permalink {mentioned}#L{lineno}")
            else:
                mod = node_id(path, MODULE_SYMBOL)
                if mod in g:
                    offer(mod, 1.6, f"permalink {mentioned}")

    # 2. Files the issue names -> their module node, so that a file mentioned
    #    with no symbol still puts its neighbourhood in play.
    for path in seed_files(text, cg):
        mod = node_id(path, MODULE_SYMBOL)
        if mod in g:
            offer(mod, 1.2, f"path {path}")

    # 3. Identifiers.
    for name, weight in _identifiers(text).items():
        hits = by_qual.get(name) or by_short.get(name.rsplit(".", 1)[-1]) or []
        if not hits or len(hits) > MAX_SEED_MATCHES:
            continue
        # A name matching several definitions splits its weight rather than
        # nominating all of them equally.
        share = weight / len(hits)
        for h in hits:
            offer(h, share, f"mentions {name}")

    return sorted(seeds.values(), key=lambda s: (-s.weight, s.node))[:max_seeds]
