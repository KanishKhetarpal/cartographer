"""Sweep retriever configurations against a fixed instance set.

Answers two questions the design left open with an opinion instead of a number:

  1. **Does the guessed half of the graph help or hurt?** Half of Python call
     edges are earned by a repo-wide name match (`unique_name` 0.5) or a
     capped fan-out (`ambiguous` 0.25). They could be carrying the recall or
     diluting it, and `min_confidence` decides which without a rebuild.
  2. **How far is worth walking?** More hops reach more, and also reach
     everything.

The graph is built once per instance and reused across every configuration --
that is what `GraphRetriever(graph=...)` is for. Building per config would make
this sweep cost O(configs x instances) parses for no extra information.

Usage:
    uv run python eval/ablation.py <swebench.json> <repo-root-dir> --repos requests,pytest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartographer.graph.graph_builder import build_graph  # noqa: E402
from cartographer.retrieval.base import Issue  # noqa: E402
from cartographer.retrieval.graph_retriever import GraphRetriever  # noqa: E402
from cartographer.retrieval.seeds import extract_seeds, seed_files  # noqa: E402
from eval.graph_hit_rate import (  # noqa: E402
    K_VALUES,
    FixtureLock,
    checkout,
    gold_files,
    rank_files,
)

#: (label, min_confidence, hops). `min_confidence` cut points sit between the
#: builder's confidence tiers, so each step drops exactly one resolution path:
#:   0.00  everything
#:   0.30  drops `ambiguous` (0.25)
#:   0.60  also drops `unique_name` (0.5) -- i.e. no guessed edges at all
#:   0.86  also drops `self_mro` (0.85) -- import-resolved edges only
CONFIGS: tuple[tuple[str, float, int], ...] = (
    ("baseline h3 c0.00", 0.00, 3),
    ("no-ambiguous h3 c0.30", 0.30, 3),
    ("no-guesses h3 c0.60", 0.60, 3),
    ("imports-only h3 c0.86", 0.86, 3),
    ("baseline h1 c0.00", 0.00, 1),
    ("baseline h2 c0.00", 0.00, 2),
    ("baseline h4 c0.00", 0.00, 4),
)


#: The no-graph control, swept alongside the configurations so that one pass
#: over the instances yields both the headline comparison and the sweep. Graph
#: construction dominates the cost (36s for django), so running the two
#: harnesses separately would double the expensive half for no new information.
SEEDS_ONLY = "seeds-only (no graph)"


def run(instances, repo_dir: Path, configs, verbose: bool, per_repo: dict[str, list] | None = None):
    labels = [SEEDS_ONLY, *(label for label, _, _ in configs)]
    totals = {label: dict.fromkeys(K_VALUES, 0.0) for label in labels}
    counted = 0
    for inst in instances:
        repo = repo_dir / inst["repo"].split("/")[-1]
        gold = gold_files(inst["patch"])
        if not gold or not repo.exists() or not checkout(repo, inst["base_commit"]):
            continue
        cg = build_graph(repo)
        text = inst["problem_statement"]
        issue = Issue(id=inst["instance_id"], title="", body=text)

        seeds = extract_seeds(text, cg)
        control: dict[str, None] = {}
        for p in seed_files(text, cg):
            control.setdefault(p, None)
        for s in sorted(seeds, key=lambda s: -s.weight):
            control.setdefault(cg.g.nodes[s.node]["path"], None)
        per_instance = {SEEDS_ONLY: list(control)}

        for label, conf, hops in configs:
            order, _ = GraphRetriever(
                max_hops=hops, min_confidence=conf, graph=cg
            ).rank(issue, cg)
            per_instance[label] = rank_files(cg, order, max(K_VALUES))

        for label, files in per_instance.items():
            for k in K_VALUES:
                totals[label][k] += len(gold & set(files[:k])) / len(gold)
        if per_repo is not None:
            per_repo.setdefault(inst["repo"], []).append(
                {
                    "id": inst["instance_id"],
                    "gold": len(gold),
                    "files": cg.stats["files"],
                    "build_s": cg.stats["build_seconds"],
                    **{
                        f"{label}@{k}": len(gold & set(files[:k])) / len(gold)
                        for label, files in per_instance.items()
                        for k in K_VALUES
                    },
                }
            )
        counted += 1
        if verbose and counted % 10 == 0:
            print(f"  {counted} instances", flush=True)
    return totals, counted


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("repo_dir")
    ap.add_argument("--repos", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--max-per-repo", type=int, default=0,
                    help="Cap instances per repo. django alone is 231 x 36s.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    wanted = {r.strip() for r in args.repos.split(",") if r.strip()}
    if wanted:
        data = [d for d in data if d["repo"].split("/")[-1] in wanted]
    if args.max_per_repo:
        # Deterministic: the dataset order, truncated. Not sampled, so a rerun
        # scores the same instances.
        seen: dict[str, int] = {}
        capped = []
        for d in data:
            n_seen = seen.get(d["repo"], 0)
            if n_seen < args.max_per_repo:
                capped.append(d)
                seen[d["repo"]] = n_seen + 1
        data = capped

    per_repo: dict[str, list] = {}
    with FixtureLock(Path(args.repo_dir)):
        totals, n = run(data, Path(args.repo_dir), CONFIGS,
                        verbose=not args.quiet, per_repo=per_repo)
    if not n:
        print("no instances evaluated")
        return

    header = f"{'config':<26}" + "".join(f"{'@' + str(k):>8}" for k in K_VALUES)
    print(f"\nn={n}\n{header}")
    rows = {}
    for label in [SEEDS_ONLY, *(c[0] for c in CONFIGS)]:
        vals = {k: totals[label][k] / n for k in K_VALUES}
        rows[label] = vals
        print(f"{label:<26}" + "".join(f"{vals[k]:>8.3f}" for k in K_VALUES))

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "n": n,
                    "repos": {k: len(v) for k, v in per_repo.items()},
                    "configs": [SEEDS_ONLY, *(c[0] for c in CONFIGS)],
                    "recall": rows,
                    "rows": per_repo,
                },
                indent=1,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
