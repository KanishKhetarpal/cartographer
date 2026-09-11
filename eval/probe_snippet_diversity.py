"""Premise check for next-actions item 1's second half in CONTEXT.md: "an issue
naming several symbols in one file currently consumes all of `k` before the
graph contributes anything."

Every hit-rate number reported so far (`graph_hit_rate.py`, `embedding_hit_rate.py`,
`ablation.py`) measures `.rank()` -- the full, *unbounded* node order collapsed to
files -- deliberately, so k isn't confounded with ranking quality (see
`embedding_retriever.py`'s module docstring). That is the right way to compare
retrievers, but it is not what an agent actually receives: `.retrieve()` stops at
`k` *snippets* (default 12), and snippets are symbol-level, so several snippets
can share one file. If a file the issue names surfaces many symbols near the top
of the ranking, `.retrieve()`'s k could be spent entirely inside that one file
while `rank_files()` at the same k would already have moved on to others.

This probes the gap directly: for each instance, build the graph once and compare
-- at the same k -- the file recall `.retrieve()` would actually *deliver*
against the file recall `rank_files()` reports for the unbounded ranking capped to
the same k. If they match, the concern is not real for the shipped defaults. If
delivered recall is meaningfully lower, the next-actions item has a real basis and
a fix (e.g. one-snippet-per-file-first, then fill) is worth building.

Usage:
    uv run python eval/probe_snippet_diversity.py <swebench.json> <repo-root-dir> \
        [--repos requests,pytest] [--k 12]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartographer.graph.graph_builder import build_graph  # noqa: E402
from cartographer.retrieval.base import Issue, RepoRef  # noqa: E402
from cartographer.retrieval.graph_retriever import GraphRetriever  # noqa: E402
from eval.graph_hit_rate import FixtureLock, checkout, gold_files, rank_files  # noqa: E402


def evaluate(instances, repo_dir: Path, *, k: int, budget: int, verbose: bool):
    rows = []
    for inst in instances:
        repo = repo_dir / inst["repo"].split("/")[-1]
        if not repo.exists():
            continue
        gold = gold_files(inst["patch"])
        if not gold:
            continue
        if not checkout(repo, inst["base_commit"]):
            rows.append({"id": inst["instance_id"], "error": "checkout failed"})
            continue

        cg = build_graph(repo)
        retriever = GraphRetriever(k=k, graph=cg)
        issue = Issue(id=inst["instance_id"], title="", body=inst["problem_statement"])

        ctx = retriever.retrieve(issue, RepoRef(root=repo), budget_tokens=budget)
        delivered = set(ctx.files)

        order, _stats = retriever.rank(issue, cg)
        ranked_at_k = set(rank_files(cg, order, k))

        row = {
            "id": inst["instance_id"],
            "repo": inst["repo"],
            "gold": sorted(gold),
            "snippets": len(ctx.snippets),
            "delivered_files": len(delivered),
            "recall_delivered": len(gold & delivered) / len(gold),
            "recall_rank_at_k": len(gold & ranked_at_k) / len(gold),
        }
        rows.append(row)
        if verbose:
            gap = row["recall_rank_at_k"] - row["recall_delivered"]
            flag = " <-- gap" if gap > 0 else ""
            d, r = row["recall_delivered"], row["recall_rank_at_k"]
            print(
                f"  {inst['instance_id']:<28} snippets={row['snippets']:<3} "
                f"files={row['delivered_files']:<3} delivered={d:.2f} rank@k={r:.2f}{flag}",
                flush=True,
            )
    return rows


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {}
    gap = [r["recall_rank_at_k"] - r["recall_delivered"] for r in ok]
    return {
        "instances": len(ok),
        "recall_delivered": round(sum(r["recall_delivered"] for r in ok) / len(ok), 4),
        "recall_rank_at_k": round(sum(r["recall_rank_at_k"] for r in ok) / len(ok), 4),
        "instances_with_gap": sum(1 for g in gap if g > 0),
        "mean_files_per_snippet_set": round(
            sum(r["delivered_files"] / r["snippets"] for r in ok if r["snippets"]) / len(ok), 4
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("repo_dir")
    ap.add_argument("--repos", default="")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--budget", type=int, default=8000)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    wanted = {r.strip() for r in args.repos.split(",") if r.strip()}
    if wanted:
        data = [d for d in data if d["repo"].split("/")[-1] in wanted]

    with FixtureLock(Path(args.repo_dir)):
        rows = evaluate(data, Path(args.repo_dir), k=args.k, budget=args.budget,
                         verbose=not args.quiet)
    print(json.dumps(summarise(rows), indent=2))


if __name__ == "__main__":
    main()
