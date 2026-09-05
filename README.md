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
| 1 | 0.280 | 0.280 |
| 3 | 0.412 | 0.412 |
| 5 | 0.531 | 0.523 |
| 10 | 0.582 | 0.557 |
| 20 | **0.740** | 0.557 |

**The graph is worth +18 points of file recall at k=20 and roughly nothing below k=5.** Since the
thesis is about *tight* context, that is a weaker result than the pitch wants, and it is reported
rather than rounded off. Closing the small-k gap is the most valuable open work here.

Reproduce with `uv run python eval/graph_hit_rate.py <swebench.json> <repo-dir> --repos requests`;
raw rows in [`results/phase1_hitrate.json`](results/phase1_hitrate.json).

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
