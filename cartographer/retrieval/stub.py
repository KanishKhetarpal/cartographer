"""Shared Phase-0 placeholder. Deleted once both real retrievers land."""

from __future__ import annotations

from .base import RepoRef, Snippet

_SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}


def stub_snippets(
    repo: RepoRef, k: int, *, reason: str, head_lines: int = 20
) -> tuple[Snippet, ...]:
    out: list[Snippet] = []
    for path in sorted(repo.root.rglob("*.py")):
        if any(part in _SKIP for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        rel = path.relative_to(repo.root).as_posix()
        span = lines[:head_lines]
        out.append(
            Snippet(
                path=rel,
                start_line=1,
                end_line=len(span),
                text="\n".join(span),
                score=1.0 - len(out) / max(k, 1),
                reason=reason,
            )
        )
        if len(out) >= k:
            break
    return tuple(out)
