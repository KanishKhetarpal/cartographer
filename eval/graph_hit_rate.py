"""Phase-1 validation: does the blast radius find where the fix actually goes?

For each SWE-bench Verified instance we know the answer -- the gold patch names
the files a correct fix touches. So: check the repo out at `base_commit`, build
the graph, seed it from the issue text alone, rank, and ask whether the gold
files are in the top k.

**The seeds-only column is the control and it is the whole point.** Lexical
seed extraction is itself a retriever: an issue that says "Response.json() is
broken" names the file without any graph at all. Reporting blast-radius recall
on its own would credit the graph for work the regex did. The number that
matters is the *difference*, and on the repos here it is frequently zero or
negative -- which is a finding, not a bug to hide.

Usage:
    uv run python eval/graph_hit_rate.py <swebench.json> <repo-root-dir> [--repos requests,pytest]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartographer.graph.graph_builder import build_graph  # noqa: E402
from cartographer.retrieval.base import Issue  # noqa: E402
from cartographer.retrieval.graph_retriever import GraphRetriever  # noqa: E402
from cartographer.retrieval.seeds import extract_seeds, seed_files  # noqa: E402

_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)

K_VALUES = (1, 3, 5, 10, 20)


def gold_files(patch: str) -> set[str]:
    return {p for p in _DIFF_FILE.findall(patch) if p.endswith(".py")}


def checkout(repo: Path, sha: str) -> bool:
    r = subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "--force", sha],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def rank_files(cg, nodes, limit: int) -> list[str]:
    """Collapse a symbol ranking to a file ranking, keeping first appearance."""
    out: dict[str, None] = {}
    for n in nodes:
        out.setdefault(cg.g.nodes[n]["path"], None)
        if len(out) >= limit:
            break
    return list(out)


def evaluate(instances, repo_dir: Path, *, hops: int, min_confidence: float, verbose: bool):
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
        text = inst["problem_statement"]
        seeds = extract_seeds(text, cg)
        # Measure the SHIPPED retriever, not a re-implementation of it. An eval
        # that scores its own copy of the ranking can drift from the code that
        # actually runs, and the drift always flatters the eval.
        retriever = GraphRetriever(max_hops=hops, min_confidence=min_confidence, graph=cg)
        order, _ = retriever.rank(Issue(id=inst["instance_id"], title="", body=text), cg)

        # Control: the files the issue literally names, plus the files the
        # seeded symbols live in. No graph traversal at all.
        control: dict[str, None] = {}
        for p in seed_files(text, cg):
            control.setdefault(p, None)
        for s in sorted(seeds, key=lambda s: -s.weight):
            control.setdefault(cg.g.nodes[s.node]["path"], None)

        graph_files = rank_files(cg, order, max(K_VALUES))
        row = {
            "id": inst["instance_id"],
            "repo": inst["repo"],
            "gold": sorted(gold),
            "n_seeds": len(seeds),
            "nodes": cg.stats["nodes"],
            "files": cg.stats["files"],
            "build_s": cg.stats["build_seconds"],
        }
        for k in K_VALUES:
            row[f"graph@{k}"] = len(gold & set(graph_files[:k])) / len(gold)
            row[f"seeds@{k}"] = len(gold & set(list(control)[:k])) / len(gold)
        rows.append(row)
        if verbose:
            print(
                f"  {inst['instance_id']:<45} gold={len(gold)} seeds={len(seeds):<3} "
                f"g@10={row['graph@10']:.2f} s@10={row['seeds@10']:.2f}",
                flush=True,
            )
    return rows


def summarise(rows):
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {}
    out = {"instances": len(ok)}
    for k in K_VALUES:
        for mode in ("graph", "seeds"):
            key = f"{mode}@{k}"
            out[f"recall_{key}"] = round(sum(r[key] for r in ok) / len(ok), 4)
            out[f"anyhit_{key}"] = round(sum(1 for r in ok if r[key] > 0) / len(ok), 4)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("repo_dir")
    ap.add_argument("--repos", default="")
    ap.add_argument("--hops", type=int, default=3)
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    wanted = {r.strip() for r in args.repos.split(",") if r.strip()}
    if wanted:
        data = [d for d in data if d["repo"].split("/")[-1] in wanted]

    rows = evaluate(
        data, Path(args.repo_dir), hops=args.hops,
        min_confidence=args.min_confidence, verbose=not args.quiet,
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
