"""Premise check for next-actions item 1 in CONTEXT.md: would a literal index on
CLI flags and message codes (`--ignore-paths`, `W0611`) actually reach pylint's
gold files?

pylint is the worst-scoring repo in the retrieval eval (~0.23 at k=20) because its
issues quote flags and message codes rather than naming identifiers -- the current
seed extractor has nothing to bind to. Those strings *do* live verbatim in the
source that implements them, so the natural next move is a literal index. **Before
building one, check whether the premise holds**: does the string occur in the gold
file, and how many other files does it also occur in? A candidate that occurs in
30 files is useless as a seed regardless of whether one of those 30 is gold.

This is a probe, not a metric -- no k, no recall number, just per-instance
candidate/gold overlap, because the question is binary (does literal seeding reach
gold at all) before it is a question of degree.

Usage:
    uv run python eval/probe_literal_seeding.py <swebench.json> <repo-root-dir>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.graph_hit_rate import FixtureLock, checkout, gold_files  # noqa: E402

# --foo-bar / --foo_bar: an option name, not a lone word -- `--verbose` alone is
# too common to be a useful literal, so this only matches multi-segment options.
FLAG = re.compile(r"(?<![\w-])--?([a-zA-Z][\w]*(?:-[\w]+)+)")
# pylint message codes: one letter (E/W/C/R/F) + four digits.
CODE = re.compile(r"\b([EWCRF]\d{4})\b")
# a bare option name inside backticks, e.g. `` `min-similarity-lines` ``.
DOTTED_OPT = re.compile(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`")

MAX_USEFUL_FILES = 8  # a candidate hitting more files than this is not a seed


def candidates(problem_statement: str) -> set[str]:
    found = set(FLAG.findall(problem_statement))
    found |= set(CODE.findall(problem_statement))
    found |= set(DOTTED_OPT.findall(problem_statement))
    return {c for c in found if len(c) >= 4}


def probe(inst: dict, repo: Path, *, verbose: bool) -> dict:
    gold = gold_files(inst["patch"])
    cands = candidates(inst["problem_statement"])

    hits: dict[str, list[str]] = {}
    for p in repo.rglob("*.py"):
        if ".git" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = p.relative_to(repo).as_posix()
        for c in cands:
            if c in text:
                hits.setdefault(c, []).append(rel)

    useful = {c: files for c, files in hits.items() if len(files) <= MAX_USEFUL_FILES}
    reached = {f for files in useful.values() for f in files}
    row = {
        "id": inst["instance_id"],
        "gold": sorted(gold),
        "candidates": sorted(cands),
        "useful_candidates": sorted(useful),
        "gold_reached": sorted(gold & reached),
    }
    if verbose:
        mark = "reached" if row["gold_reached"] else "MISSED"
        print(f"  {inst['instance_id']:<28} cands={len(cands):<3} useful={len(useful):<3} {mark}")
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("repo_dir")
    ap.add_argument("--repo-name", default="pylint")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    insts = [d for d in data if d["repo"].split("/")[-1] == args.repo_name]
    repo = Path(args.repo_dir) / args.repo_name

    rows = []
    with FixtureLock(Path(args.repo_dir)):
        for inst in insts:
            if not checkout(repo, inst["base_commit"]):
                continue
            rows.append(probe(inst, repo, verbose=not args.quiet))

    reached = sum(1 for r in rows if r["gold_reached"])
    print(f"\n{reached}/{len(rows)} instances reached by literal seeding "
          f"(candidate in <= {MAX_USEFUL_FILES} files, one of them gold)")


if __name__ == "__main__":
    main()
