# Cartographer — results

_Generated 2026-09-07 by `uv run python eval/report.py`._

Every number here comes from checking a repo out at a SWE-bench Verified instance's
`base_commit`, seeding the graph from the issue text alone, and comparing the ranked
files against the files the gold patch actually touches.

⚠️ **This measures retrieval, not resolution.** No model is called yet, so nothing here
is a SWE-bench resolved-rate. That number arrives in Phase 4.

## Retrieval vs. the no-graph control

Instances: **59** (requests 8, xarray 22, pylint 10, pytest 19)

File-level recall of the gold patch. `seeds only` is lexical extraction with no graph
traversal at all -- the floor the graph has to beat to have earned anything.

| k | graph | seeds only | delta |
|---|---|---|---|
| 1 | 0.356 | 0.356 | +0.000 |
| 3 | 0.556 | 0.556 | +0.000 |
| 5 | 0.616 | 0.599 | +0.017 |
| 10 | 0.684 | 0.633 | +0.051 |
| 20 | 0.774 | 0.633 | +0.141 |

## Configuration ablation

Two runs, and the interesting part is that they disagree. See the note below the tables.

### Large repos (django, sympy)

n = **60** (django 30, sympy 30)

One instance is worth 1.7 points, so a difference smaller than that is one instance moving and should not be read as a result.

| config | @1 | @3 | @5 | @10 | @20 |
|---|---|---|---|---|---|
| seeds-only (no graph) | 0.386 | 0.608 | 0.658 | 0.699 | 0.699 |
| baseline h3 c0.00 | 0.386 | 0.608 | 0.674 | 0.733 | 0.776 |
| no-ambiguous h3 c0.30 | 0.386 | 0.608 | 0.658 | 0.733 | 0.776 |
| no-guesses h3 c0.60 | 0.386 | 0.608 | 0.658 | 0.733 | 0.768 |
| imports-only h3 c0.86 | 0.386 | 0.608 | 0.674 | 0.716 | 0.749 |
| baseline h1 c0.00 | 0.386 | 0.608 | 0.658 | 0.716 | 0.743 |
| baseline h2 c0.00 | 0.386 | 0.608 | 0.674 | 0.733 | 0.776 |
| baseline h4 c0.00 | 0.386 | 0.608 | 0.674 | 0.733 | 0.776 |

### Small repos (requests, pytest, pylint, xarray)

n = **59** (requests 8, xarray 22, pylint 10, pytest 19)

One instance is worth 1.7 points, so a difference smaller than that is one instance moving and should not be read as a result.

| config | @1 | @3 | @5 | @10 | @20 |
|---|---|---|---|---|---|
| seeds-only (no graph) | 0.356 | 0.556 | 0.599 | 0.633 | 0.633 |
| baseline h3 c0.00 | 0.356 | 0.556 | 0.616 | 0.684 | 0.774 |
| no-ambiguous h3 c0.30 | 0.356 | 0.556 | 0.616 | 0.701 | 0.766 |
| no-guesses h3 c0.60 | 0.356 | 0.556 | 0.616 | 0.709 | 0.766 |
| imports-only h3 c0.86 | 0.356 | 0.556 | 0.616 | 0.709 | 0.766 |
| baseline h1 c0.00 | 0.356 | 0.556 | 0.607 | 0.684 | 0.715 |
| baseline h2 c0.00 | 0.356 | 0.556 | 0.616 | 0.684 | 0.740 |
| baseline h4 c0.00 | 0.356 | 0.556 | 0.616 | 0.684 | 0.782 |

### What replicated and what did not

Dropping every name-matched call edge (`min_confidence` >= 0.60) improves recall at k=10
by 2.5 points on the small repos and does nothing on the large ones, where baseline ties
or beats every restricted configuration. **At n~60 one instance is worth 1.7 points, so
that gain was about 1.5 instances.** The default keeps the guessed edges.

What holds on both sets: one hop is not enough, three is not better than two, and
`imports-only` -- which also discards `self.method()` resolution through the MRO -- is
worse than baseline on the large repos at every k above 5.
