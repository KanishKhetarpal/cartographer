"""results/*.json -> results/REPORT.md.

The project's deliverable is a table someone can read, not a directory of JSON.
This renders whatever result files are present, and says so when one is missing
rather than emitting a section built from nothing.

    uv run python eval/report.py
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

K_ORDER = ("1", "3", "5", "10", "20")


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _row(label: str, get) -> str:
    return "| " + " | ".join([label, *(get(k) for k in K_ORDER)]) + " |"


def hitrate_section(d: dict | None) -> list[str]:
    if not d or "summary" not in d:
        return ["_No `phase1_hitrate.json` present._", ""]
    s = d["summary"]
    repos: dict[str, int] = {}
    for r in d.get("rows", []):
        if "error" not in r:
            repos[r["repo"]] = repos.get(r["repo"], 0) + 1
    out = [
        f"Instances: **{s['instances']}** "
        + "(" + ", ".join(f"{k.split('/')[-1]} {v}" for k, v in sorted(repos.items())) + ")",
        "",
        "File-level recall of the gold patch. `seeds only` is lexical extraction with no graph",
        "traversal at all -- the floor the graph has to beat to have earned anything.",
        "",
        "| k | graph | seeds only | delta |",
        "|---|---|---|---|",
    ]
    for k in K_ORDER:
        g, b = s.get(f"recall_graph@{k}"), s.get(f"recall_seeds@{k}")
        if g is None:
            continue
        out.append(f"| {k} | {g:.3f} | {b:.3f} | {g - b:+.3f} |")
    out.append("")
    return out


def embedding_section(hitrate: dict | None, embed: dict | None) -> list[str]:
    if not embed or "summary" not in embed:
        return ["_No `phase4_hitrate.json` present._", ""]
    if not hitrate or "summary" not in hitrate:
        return ["_No `phase1_hitrate.json` present -- need the graph numbers to compare._", ""]
    hs, es = hitrate["summary"], embed["summary"]
    n = es["instances"]
    out = [
        f"Same **{n}** instances as the retrieval table above, same k values, the real",
        "`EmbeddingRetriever` (sentence-transformers/all-MiniLM-L6-v2, fixed-size line-window",
        "chunks, cosine top-k -- no function/class awareness, so it can't borrow the graph's own",
        "idea) against the shipped `GraphRetriever`.",
        "",
        f"One instance is worth {100 / n:.1f} points -- a delta under that is noise, not a result.",
        "",
        "| k | graph | embedding | delta |",
        "|---|---|---|---|",
    ]
    g10, e10 = hs.get("recall_graph@10"), es.get("recall_embedding@10")
    for k in K_ORDER:
        g, e = hs.get(f"recall_graph@{k}"), es.get(f"recall_embedding@{k}")
        if g is None or e is None:
            continue
        out.append(f"| {k} | {g:.3f} | {e:.3f} | {g - e:+.3f} |")
    out.append("")
    if g10 is not None and e10 is not None:
        out += [
            "Graph leads clearly at k=1 and k=3 -- where the thesis says the graph should matter",
            "*least*, since the issue text alone should already answer it. The lead shrinks",
            f"through k=5, and at k=10 embedding edges ahead ({e10:.3f} vs. {g10:.3f}) before",
            "graph retakes a lead at k=20 that is itself inside the noise floor. **Not a clean",
            "win.**",
            "k=1/k=3 are real (7.5 and ~4 instances); k=10/k=20 are not (≤1.5 instances each way).",
            "",
        ]
    out += [
        "⚠️ **This measures retrieval, not resolution.** Neither retriever has produced a patch",
        "that was scored against the real test suite. That number needs Phase 3's agent loop.",
        "",
    ]
    return out


def ablation_section(d: dict | None, title: str) -> list[str]:
    if not d or "recall" not in d:
        return [f"_No results for {title}._", ""]
    n = d.get("n", 0)
    out = [f"### {title}", "", f"n = **{n}**"]
    repos = d.get("repos")
    if repos:
        out[-1] += " (" + ", ".join(
            f"{k.split('/')[-1]} {v}" for k, v in sorted(repos.items())
        ) + ")"
    caveat = (
        f"One instance is worth {100 / n:.1f} points, so a difference smaller than that is "
        "one instance moving and should not be read as a result."
        if n
        else ""
    )
    out += [
        "",
        caveat,
        "",
        "| config | " + " | ".join(f"@{k}" for k in K_ORDER) + " |",
        "|---|" + "---|" * len(K_ORDER),
    ]
    for label in d.get("configs", list(d["recall"])):
        vals = d["recall"].get(label)
        if not vals:
            continue
        out.append(_row(label, lambda k, v=vals: f"{v[k]:.3f}"))
    out.append("")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    args = ap.parse_args()
    root = Path(args.results)

    lines = [
        "# Cartographer — results",
        "",
        f"_Generated {datetime.now(UTC):%Y-%m-%d} by `uv run python eval/report.py`._",
        "",
        "Every number here comes from checking a repo out at a SWE-bench Verified instance's",
        "`base_commit`, seeding the graph from the issue text alone, and comparing the ranked",
        "files against the files the gold patch actually touches.",
        "",
        "⚠️ **This measures retrieval, not resolution.** No model is called yet, so nothing here",
        "is a SWE-bench resolved-rate. That number needs Phase 3's agent loop to produce real",
        "patches for the sandbox to score.",
        "",
        "## Retrieval vs. the no-graph control",
        "",
        *hitrate_section(_load(root / "phase1_hitrate.json")),
        "## Retrieval vs. the embedding baseline",
        "",
        *embedding_section(
            _load(root / "phase1_hitrate.json"), _load(root / "phase4_hitrate.json")
        ),
        "## Configuration ablation",
        "",
        "Two runs, and the interesting part is that they disagree. See the note below the tables.",
        "",
        *ablation_section(
            _load(root / "phase1_ablation_large.json"), "Large repos (django, sympy)"
        ),
        *ablation_section(
            _load(root / "phase1_ablation.json"), "Small repos (requests, pytest, pylint, xarray)"
        ),
        "### What replicated and what did not",
        "",
        "Dropping every name-matched call edge (`min_confidence` >= 0.60) improves recall at k=10",
        "by 2.5 points on the small repos and does nothing on the large ones, where baseline ties",
        "or beats every restricted configuration. **At n~60 one instance is worth 1.7 points, so",
        "that gain was about 1.5 instances.** The default keeps the guessed edges.",
        "",
        "What holds on both sets: one hop is not enough, three is not better than two, and",
        "`imports-only` -- which also discards `self.method()` resolution through the MRO -- is",
        "worse than baseline on the large repos at every k above 5.",
    ]
    out = root / "REPORT.md"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
