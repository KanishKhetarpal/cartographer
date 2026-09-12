"""Why is measured recall identical between graph and seeds-only at k=1 and
k=3, on every one of 59 instances (0/59 diverge -- not just tied in aggregate,
literally the same per instance; 2/59 at k=5, 4/59 at k=10, 11/59 at k=20)?

Two explanations are possible: the graph's ranking is *coincidentally* no
better at small k, or it is *structurally* unable to differ there. This
probes which: evidence-first ordering (CONTEXT.md invariant #3) pins seed
files ahead of anything inferred, and blast_radius gives seed nodes the
highest injected weight, so the graph's own top-ranked *files* are typically
seed-associated files too -- the same set the seeds-only control already
ranks first, just by a different route (lexical weight vs. graph score).
`rank_files()` collapsing to first-appearance means the graph cannot show a
genuinely inferred file until its ranking runs past every seed-associated
file, regardless of how good the graph's placement of *those* files is.

For each instance this finds the rank of the first file in the graph's
ranking that is NOT in the control's evidence-file set at all -- the first
position where the graph could possibly diverge from seeds-only, structurally
-- and compares that to the smallest k in K_VALUES where recall actually did.

Usage:
    uv run python eval/probe_small_k_structure.py <swebench.json> <repo-root-dir> \
        [--repos requests,pytest]
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
from eval.graph_hit_rate import FixtureLock, checkout, gold_files, rank_files  # noqa: E402

MAX_K = 20


def evidence_files(text: str, cg) -> list[str]:
    """Same construction as graph_hit_rate.py's `control` -- the seeds-only
    retriever's own file order. Kept identical on purpose: this probe is
    about whether the graph's ranking can differ from this set, so the set
    itself must be exactly what seeds-only actually ranks."""
    out: dict[str, None] = {}
    for p in seed_files(text, cg):
        out.setdefault(p, None)
    for s in sorted(extract_seeds(text, cg), key=lambda s: -s.weight):
        out.setdefault(cg.g.nodes[s.node]["path"], None)
    return list(out)


def evaluate(instances, repo_dir: Path, *, verbose: bool):
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
        evidence = evidence_files(text, cg)

        retriever = GraphRetriever(graph=cg)
        issue = Issue(id=inst["instance_id"], title="", body=text)
        order, _stats = retriever.rank(issue, cg)
        graph_files = rank_files(cg, order, MAX_K)

        first_novel = next(
            (i for i, p in enumerate(graph_files, start=1) if p not in evidence), None
        )
        row = {
            "id": inst["instance_id"],
            "n_evidence_files": len(evidence),
            "first_novel_file_rank": first_novel,
        }
        rows.append(row)
        if verbose:
            fn = row["first_novel_file_rank"]
            print(
                f"  {inst['instance_id']:<28} evidence_files={row['n_evidence_files']:<3} "
                f"first_novel_rank={fn}",
                flush=True,
            )
    return rows


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {}
    ranks = sorted(r["first_novel_file_rank"] for r in ok if r["first_novel_file_rank"])
    n = len(ranks)
    return {
        "instances": len(ok),
        "instances_with_no_novel_file_in_top_20": sum(
            1 for r in ok if r["first_novel_file_rank"] is None
        ),
        "first_novel_file_rank_median": ranks[n // 2] if n else None,
        "first_novel_file_rank_p25": ranks[n // 4] if n else None,
        "first_novel_file_rank_p75": ranks[(3 * n) // 4] if n else None,
        "instances_where_first_novel_rank_le_3": sum(1 for r in ranks if r <= 3),
        "instances_where_first_novel_rank_le_5": sum(1 for r in ranks if r <= 5),
        "instances_where_first_novel_rank_le_10": sum(1 for r in ranks if r <= 10),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("repo_dir")
    ap.add_argument("--repos", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    wanted = {r.strip() for r in args.repos.split(",") if r.strip()}
    if wanted:
        data = [d for d in data if d["repo"].split("/")[-1] in wanted]

    with FixtureLock(Path(args.repo_dir)):
        rows = evaluate(data, Path(args.repo_dir), verbose=not args.quiet)
    print(json.dumps(summarise(rows), indent=2))


if __name__ == "__main__":
    main()
