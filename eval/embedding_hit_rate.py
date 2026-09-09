"""Phase-4 comparison: does the graph beat the embedding baseline at finding
the gold files?

Same shape as `graph_hit_rate.py` -- same instances, same `K_VALUES`, same
`gold_files()` / `checkout()` / `FixtureLock` -- because the whole point of
this eval is a fair, apples-to-apples comparison. A script that measured the
two retrievers differently could not be trusted to say which one is better,
only which measurement flatters which.

**This measures the SHIPPED `EmbeddingRetriever`**, not a re-implementation --
same discipline as `graph_hit_rate.py`, for the same reason: a copy of the
ranking logic can drift from what actually runs, and the drift always
flatters the eval.

Requires the `embedding` extra (`uv sync --extra embedding`) -- not installed
by base CI. This script is a manual/local run, like `cartographer score` is
for the sandbox.

Usage:
    uv run python eval/embedding_hit_rate.py <swebench.json> <repo-root-dir> \
        [--repos requests,pytest] [--chunk-lines 40]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartographer.retrieval.base import Issue  # noqa: E402
from cartographer.retrieval.embedding_retriever import EmbeddingRetriever  # noqa: E402
from eval.graph_hit_rate import (  # noqa: E402
    K_VALUES,
    FixtureLock,
    checkout,
    gold_files,
)


def rank_files(order: list[str], limit: int) -> list[str]:
    """First-appearance file order from a chunk-path sequence -- same collapse
    `graph_hit_rate.rank_files` does for symbol nodes, applied to chunk paths
    instead."""
    out: dict[str, None] = {}
    for p in order:
        out.setdefault(p, None)
        if len(out) >= limit:
            break
    return list(out)


def evaluate(instances, repo_dir: Path, *, chunk_lines: int, verbose: bool):
    from cartographer.retrieval.base import RepoRef

    rows = []
    retriever = EmbeddingRetriever(chunk_lines=chunk_lines)
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

        t0 = time.perf_counter()
        # .rank(), not .retrieve(): the full chunk order, unfiltered by k or a
        # token budget. Many chunks can share one file, so a top-k slice of
        # *chunks* (what .retrieve() returns) can under-count file recall --
        # exactly the reason GraphRetriever separates rank() from retrieve().
        chunks, _scores, order = retriever.rank(
            Issue(id=inst["instance_id"], title="", body=inst["problem_statement"]),
            RepoRef(root=repo),
        )
        elapsed = time.perf_counter() - t0

        ranked = rank_files([chunks[int(i)].path for i in order], max(K_VALUES))
        row = {
            "id": inst["instance_id"],
            "repo": inst["repo"],
            "gold": sorted(gold),
            "chunks": len(chunks),
            "seconds": round(elapsed, 2),
        }
        for k in K_VALUES:
            row[f"embedding@{k}"] = len(gold & set(ranked[:k])) / len(gold)
        rows.append(row)
        if verbose:
            print(
                f"  {inst['instance_id']:<45} chunks={row['chunks']:<6} "
                f"{elapsed:>6.1f}s  e@10={row['embedding@10']:.2f}",
                flush=True,
            )
    return rows


def summarise(rows):
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {}
    out = {"instances": len(ok)}
    for k in K_VALUES:
        key = f"embedding@{k}"
        out[f"recall_{key}"] = round(sum(r[key] for r in ok) / len(ok), 4)
        out[f"anyhit_{key}"] = round(sum(1 for r in ok if r[key] > 0) / len(ok), 4)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("repo_dir")
    ap.add_argument("--repos", default="")
    ap.add_argument("--chunk-lines", type=int, default=40)
    ap.add_argument("--out", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    wanted = {r.strip() for r in args.repos.split(",") if r.strip()}
    if wanted:
        data = [d for d in data if d["repo"].split("/")[-1] in wanted]

    with FixtureLock(Path(args.repo_dir)):
        rows = evaluate(
            data, Path(args.repo_dir), chunk_lines=args.chunk_lines, verbose=not args.quiet
        )
    summary = summarise(rows)
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps({"summary": summary, "config": vars(args), "rows": rows}, indent=1),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
