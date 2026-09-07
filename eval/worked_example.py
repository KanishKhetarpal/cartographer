"""Show, for one SWE-bench instance, exactly how the ranking got where it got.

This is the README's worked example, kept as a script so the numbers in the
README can be regenerated rather than remembered.

    uv run python eval/worked_example.py <swebench.json> <repo-dir> \
        pytest-dev__pytest-7236
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartographer.graph.blast_radius import blast_radius  # noqa: E402
from cartographer.graph.graph_builder import build_graph  # noqa: E402
from cartographer.retrieval.base import Issue, RepoRef  # noqa: E402
from cartographer.retrieval.graph_retriever import GraphRetriever  # noqa: E402
from cartographer.retrieval.seeds import extract_seeds, seed_files  # noqa: E402
from eval.graph_hit_rate import FixtureLock, checkout, gold_files, rank_files  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("repo_dir")
    ap.add_argument("instance_id")
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    inst = next(d for d in data if d["instance_id"] == args.instance_id)
    repo = Path(args.repo_dir) / inst["repo"].split("/")[-1]
    gold = gold_files(inst["patch"])

    with FixtureLock(Path(args.repo_dir)):
        if not checkout(repo, inst["base_commit"]):
            raise SystemExit(f"could not check out {inst['base_commit']}")
        cg = build_graph(repo)
        text = inst["problem_statement"]
        issue = Issue(id=inst["instance_id"], title="", body=text)
        seeds = extract_seeds(text, cg)
        weights = {s.node: s.weight for s in seeds}
        br = blast_radius(cg, list(weights), seed_weights=weights)
        order, stats = GraphRetriever(graph=cg).rank(issue, cg)
        ctx = GraphRetriever(k=args.k, graph=cg).retrieve(issue, RepoRef(root=cg.root))

    print(f"# {inst['instance_id']}  ({inst['repo']})")
    print(f"repo at {inst['base_commit'][:10]}: {cg.stats['files']} files, "
          f"{cg.stats['nodes']} nodes, {cg.stats['edges']} edges, "
          f"built in {cg.stats['build_seconds']}s")
    print(f"gold patch touches: {sorted(gold)}")

    print(f"\n## Files the issue literally names ({len(seed_files(text, cg))})")
    for p in seed_files(text, cg) or ["(none)"]:
        print(f"  {p}")

    print(f"\n## Seeds extracted ({len(seeds)})")
    for s in seeds[:10]:
        print(f"  {s.weight:5.2f}  {s.node:<58} {s.evidence}")

    print(f"\n## Ranked files (top {args.k} of {cg.stats['files']})")
    reason_by_node = {r.node: r for r in br.ranked}
    shown: set[str] = set()
    for node in order:
        path = cg.g.nodes[node]["path"]
        if path in shown:
            continue
        shown.add(path)
        r = reason_by_node.get(node)
        mark = "GOLD" if path in gold else "    "
        why = r.reasons[0] if r and r.reasons else "named in issue"
        print(f"  {len(shown):>2}. {mark} {path:<44} {why}")
        if len(shown) >= args.k:
            break

    files = rank_files(cg, order, args.k)
    print(f"\nrecall@{args.k} = {len(gold & set(files)) / len(gold):.2f}")
    print(f"context: {len(ctx.snippets)} snippets, ~{ctx.token_estimate} tokens")



if __name__ == "__main__":
    main()
