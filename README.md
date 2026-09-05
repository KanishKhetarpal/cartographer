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

**Phase 0 — scaffold.** Interfaces and CLI wiring are real and tested. Both retrievers are
placeholders that flag themselves (`Context.stats["stub"] is True`) so a Phase-0 run can never be
mistaken for a result. No model is called yet.

| Phase | | |
|---|---|---|
| 0 | Scaffold: interfaces, stub CLI, tests | ✅ |
| 1 | Code graph engine (Python analyzer, blast radius) | in progress |
| 2 | Docker sandbox + SWE-bench harness wiring | |
| 3 | LangGraph agent loop | |
| 4 | Embedding baseline + the ≥50-task comparison | |
| 5 | README polish, CI, architecture diagram | |
| 6 | Stretch: MCP server | |

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
