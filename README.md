# Cartographer

**A graph-grounded autonomous code agent.** Given a GitHub issue and a repo, it selects the
minimal relevant context using a *program dependency graph* — not a whole-file dump, not
embedding top-k — then plans a fix, edits, runs the repo's real tests in Docker, iterates, and
opens a PR.

## The thesis

Wiring an LLM into a fix-my-code loop is table stakes. On real repos those loops fail the same
two ways: they stuff whole files into the prompt until context blows up and accuracy craters, or
they do naive embedding retrieval and miss the call sites that actually break.

**The contribution is the retriever.** Cartographer parses the repo into a directed graph of
symbols with import, call and inheritance edges, and — given seeds extracted from the issue —
returns the ranked *blast-radius subgraph*: the functions a correct fix must touch. The agent
reasons over that.

The number this project exists to produce, honestly:

> Grounding the agent's context in a program dependency graph lifts SWE-bench Verified
> resolved-rate by **N points** over an embedding-retrieval baseline, using the same model and
> the same agent loop.

If the graph does not beat the baseline, that is a finding and it gets reported as one.

## Status

**Phase 1 — the graph engine is real.** A repo is parsed into a symbol graph with import, call and
inheritance edges; an issue is seeded lexically and expanded into a ranked blast radius. No model is
called yet: the agent loop still emits a placeholder patch, and `Result.stub` says so.

| Phase | | |
|---|---|---|
| 0 | Scaffold: interfaces, stub CLI, tests | ✅ |
| 1 | Code graph engine (Python analyzer, resolver, blast radius) | core ✅ |
| 2 | Docker sandbox + SWE-bench harness wiring | |
| 3 | LangGraph agent loop | |
| 4 | Embedding baseline + the ≥50-task comparison | |
| 5 | README polish, CI, architecture diagram | |
| 6 | Stretch: MCP server | |

### Where the graph stands today

Validated against **59 SWE-bench Verified instances** (requests, pytest, pylint, xarray) by checking
each repo out at its `base_commit`, seeding from the issue text alone, and asking whether the gold
patch's files land in the top *k*. The control is lexical seed extraction with no graph traversal —
because an issue that names a file has already told you the answer, and a retriever has to beat that
to have earned anything.

| k | graph | seeds only |
|---|---|---|
| 1 | 0.356 | 0.356 |
| 3 | 0.557 | 0.557 |
| 5 | 0.616 | 0.599 |
| 10 | **0.684** | 0.633 |
| 20 | **0.774** | 0.633 |

**The graph contributes nothing at k≤3 and +5 to +14 points from k=5 out.** Below k=3 the issue's
own text is already the whole answer. That is a weaker claim than a thesis about tight context
wants, and it is reported rather than rounded off.

Reproduce with `uv run python eval/graph_hit_rate.py <swebench.json> <repo-dir> --repos requests`;
raw rows in [`results/phase1_hitrate.json`](results/phase1_hitrate.json).

### Worked example: a file the issue never mentions

`pytest-dev__pytest-7236`. The issue names **no file at all**, so lexical retrieval has nothing to
go on and scores 0.00 at every k. The graph reaches the gold file at rank 9 of 217:

```
## Seeds extracted (6)
   1.50  src/_pytest/outcomes.py::skip              mentions unittest.skip
   1.50  src/_pytest/debugging.py::post_mortem      mentions post_mortem
   ...

## Ranked files (top 10 of 217)
   1.      src/_pytest/debugging.py     seed
   2.      src/_pytest/outcomes.py      seed
   ...
   8.      src/_pytest/skipping.py      calls from src/_pytest/outcomes.py:skip @1
   9. GOLD src/_pytest/unittest.py      calls from src/_pytest/outcomes.py:skip @1

recall@10 = 1.00
context: 10 snippets, ~604 tokens
```

The issue says `unittest.skip`; `skip` resolves to `outcomes.py::skip`; the file that a correct fix
touches is one call edge away from it. **604 tokens of context out of a 217-file repo** — which is
the discipline the whole project is about.

Regenerate with
`uv run python eval/worked_example.py <swebench.json> <repo-dir> pytest-dev__pytest-7236`.

### The guessed half of the call graph is not paying for itself

Half of Python call edges are earned by a repo-wide name match rather than by resolution.
`min_confidence` drops them without rebuilding, so the question gets a number
([`results/phase1_ablation.json`](results/phase1_ablation.json), same 59 instances):

| config | @5 | @10 | @20 |
|---|---|---|---|
| all edges | 0.616 | 0.684 | **0.774** |
| drop `ambiguous` (0.25) | 0.616 | 0.701 | 0.766 |
| drop all guesses (≥0.60) | 0.616 | **0.709** | 0.766 |
| import-resolved only (≥0.86) | 0.616 | 0.709 | 0.766 |
| 1 hop | 0.607 | 0.684 | 0.715 |
| 2 hops | 0.616 | 0.684 | 0.740 |
| 4 hops | 0.616 | 0.684 | 0.782 |

Guessed edges **cost** 2.5 points at k=10 and buy 0.8 at k=20 — they add reach and dilute precision,
and at tight k the dilution wins. ⚠️ On n=59 one instance is worth 1.7 points, so that difference is
about 1.5 instances: suggestive, not settled, and the default is unchanged until it is re-run with
more power. Walking further saturates by 3 hops.

### Why call edges carry a confidence

Python call resolution is undecidable without type inference, so the graph records how it earned
each edge instead of pretending. Measured over flask's 3963 call sites, only ~10% are a bare name
bound by an import or a local def, and a third of receivers are local variables no name-based scheme
can resolve. Edges are tiered `direct` / `attribute` / `self_mro` / `unique_name` / `ambiguous`, and
above three candidates **no edge is emitted at all** — `get` alone is called at 385 sites in flask,
and noise in a blast radius is worse than a missing edge, because it silently spends the token
budget on the wrong files.

## Quickstart

```bash
uv sync --group dev
uv run cartographer resolve --repo /path/to/checkout --issue issue.txt --mode graph
uv run pytest
```

`--issue` takes either plain text (first non-empty line is the title) or a JSON object with
`problem_statement` — the shape SWE-bench hands out.

## Architecture

```
                ┌─────────────────────────────────────────────┐
   GitHub       │                 ORCHESTRATOR                 │
   issue  ─────▶│         (LangGraph state machine)            │
                │  plan → retrieve → edit → test → reflect ↺   │
                └───────┬───────────────┬───────────────┬──────┘
                        │               │               │
                ┌───────▼──────┐ ┌──────▼───────┐ ┌─────▼────────┐
                │  RETRIEVER   │ │  CODE EDITOR │ │   SANDBOX    │
                │ graph | embed│ │ apply patch  │ │ Docker: run  │
                │ (pluggable)  │ │ + validate   │ │ repo tests   │
                └───────┬──────┘ └──────────────┘ └──────────────┘
                        │
                ┌───────▼───────────────────────────────────────┐
                │        CODE GRAPH ENGINE (the moat)           │
                │  parse → symbol table →                       │
                │  import + call + inheritance graph →          │
                │  blast-radius ranking (fan-in/out, distance)  │
                └───────────────────────────────────────────────┘
```

Two design lines worth stating up front, because both cost something to hold:

- **A retriever's only channel to the agent is `Context`.** Same dataclass, same fields, both
  modes. If the loop could branch on which retriever it was handed, the comparison would be
  measuring the branch instead of the graph.
- **Analyzers are file-local and syntactic; the graph builder owns every cross-file
  resolution.** A new language costs one `LanguageAnalyzer`, not a new builder.

## License

MIT
