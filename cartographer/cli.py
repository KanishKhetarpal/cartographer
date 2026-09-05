"""Command line entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .agent.orchestrator import resolve as run_resolve
from .retrieval import MODES, RepoRef, build_retriever
from .retrieval.base import Issue

app = typer.Typer(add_completion=False, help="Graph-grounded autonomous code agent.")
console = Console()


def _load_issue(path: Path) -> Issue:
    """Accept either a JSON object or plain text.

    Plain text is the common case when a human pastes an issue into a file, so
    it must not require ceremony: the first non-empty line is the title.
    """
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(raw)
        return Issue(
            id=str(data.get("id") or data.get("instance_id") or path.stem),
            title=str(data.get("title", "")),
            body=str(data.get("body") or data.get("problem_statement") or ""),
        )
    lines = raw.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.strip()), 0)
    return Issue(
        id=path.stem,
        title=lines[first].strip() if lines else "",
        body="\n".join(lines[first + 1 :]).strip(),
    )


@app.command()
def resolve(
    repo: Path = typer.Option(..., "--repo", exists=True, file_okay=False, help="Repo checkout."),
    issue: Path = typer.Option(..., "--issue", exists=True, dir_okay=False, help="Issue file."),
    mode: str = typer.Option("graph", "--mode", help=f"Retrieval mode: {'|'.join(sorted(MODES))}."),
    k: int = typer.Option(12, "--k", help="Snippets to retrieve."),
    budget_tokens: int = typer.Option(8000, "--budget-tokens"),
    out: Path | None = typer.Option(None, "--out", help="Write the patch here instead of stdout."),
) -> None:
    """Resolve an issue against a repo checkout and emit a patch."""
    if mode not in MODES:
        console.print(f"[red]unknown mode {mode!r}[/] -- expected one of {sorted(MODES)}")
        raise typer.Exit(2)

    result = run_resolve(
        _load_issue(issue),
        RepoRef(root=repo),
        build_retriever(mode, k=k),
        budget_tokens=budget_tokens,
    )

    table = Table(title=f"{result.issue_id}  ·  mode={result.mode}", show_header=True)
    table.add_column("step")
    table.add_column("detail")
    table.add_column("s", justify="right")
    for step in result.trace:
        table.add_row(step.node, step.detail, f"{step.elapsed_s:.3f}")
    console.print(table)
    console.print(
        f"context: {len(result.context.snippets)} snippets, "
        f"~{result.context.token_estimate} tokens, {len(result.context.files)} files"
    )
    if result.stub:
        console.print("[yellow]STUB RUN[/] — no model was called; this patch is a placeholder.")

    if out:
        out.write_text(result.patch or "", encoding="utf-8")
        console.print(f"patch written to {out}")
    else:
        sys.stdout.write(result.patch or "")


@app.command()
def modes() -> None:
    """List available retrieval modes."""
    for name in sorted(MODES):
        console.print(name)


if __name__ == "__main__":
    app()
