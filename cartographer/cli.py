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


@app.command()
def score(
    instance_id: str = typer.Option(..., "--instance-id", help="A SWE-bench Verified id."),
    patch: Path | None = typer.Option(
        None, "--patch", exists=True, dir_okay=False,
        help="Unified diff to grade. Omit with --gold to grade the dataset's own patch.",
    ),
    gold: bool = typer.Option(
        False, "--gold", help="Grade the dataset's own patch (the harness sanity check)."
    ),
    run_id: str = typer.Option(..., "--run-id"),
    model_name: str = typer.Option("cartographer", "--model-name"),
    report_dir: Path = typer.Option(Path("results/sandbox"), "--report-dir"),
    timeout: int = typer.Option(1800, "--timeout"),
) -> None:
    """Score one patch against one SWE-bench instance through the real Docker
    harness. This is the Phase-2 acceptance check made runnable by hand:
    confirm the verdict pipeline works before trusting it inside an eval loop.
    """
    from .sandbox.docker_runner import Prediction, write_predictions
    from .sandbox.docker_runner import run as run_harness

    if gold == bool(patch):
        console.print("[red]exactly one of --patch or --gold is required[/]")
        raise typer.Exit(2)

    report_dir.mkdir(parents=True, exist_ok=True)
    if gold:
        predictions_path: str | Path = "gold"
    else:
        patch_text = patch.read_text(encoding="utf-8")  # type: ignore[union-attr]
        predictions_path = write_predictions(
            [Prediction(instance_id=instance_id, patch=patch_text, model_name=model_name)],
            report_dir / f"{run_id}.preds.json",
        )

    console.print(f"scoring {instance_id} ({'gold' if gold else patch}) ...")
    report = run_harness(
        predictions_path,
        run_id=run_id,
        instance_ids=[instance_id],
        report_dir=report_dir,
        max_workers=1,
        timeout=timeout,
    )
    verdict = "RESOLVED" if instance_id in report.resolved_ids else "unresolved"
    colour = "green" if verdict == "RESOLVED" else "red"
    console.print(f"[{colour}]{verdict}[/] — {instance_id}")
    if instance_id in report.error_ids:
        console.print(
            "[red]this instance errored — see the harness log under logs/run_evaluation/[/]"
        )


@app.command()
def graph(
    repo: Path = typer.Option(..., "--repo", exists=True, file_okay=False),
    fmt: str = typer.Option("mermaid", "--format", help="mermaid | dot | summary"),
    out: Path | None = typer.Option(None, "--out"),
    exclude: str = typer.Option("", "--exclude", help="Comma-separated path fragments to drop."),
    max_nodes: int = typer.Option(60, "--max-nodes"),
) -> None:
    """Draw the repo's module import graph.

    Module granularity, not symbol: 46554 nodes on django is a retrieval index,
    not a picture.
    """
    from .graph.graph_builder import build_graph
    from .graph.render import module_summary, to_dot, to_mermaid

    cg = build_graph(repo)
    skip = tuple(p.strip() for p in exclude.split(",") if p.strip())

    if fmt == "summary":
        table = Table(title=f"{cg.stats['files']} modules by fan-in")
        table.add_column("module")
        table.add_column("fan-in", justify="right")
        table.add_column("fan-out", justify="right")
        for path, fin, fout in module_summary(cg)[:25]:
            table.add_row(path, str(fin), str(fout))
        console.print(table)
        return

    try:
        text = to_dot(cg) if fmt == "dot" else to_mermaid(cg, exclude=skip, max_nodes=max_nodes)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None

    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"wrote {out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    app()
