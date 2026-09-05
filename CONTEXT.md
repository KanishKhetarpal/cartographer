# Cartographer — session handover

Working context for whoever (or whichever session) picks this up next. Read this first, then
`README.md` for the pitch and `cartographer_kickoff.md` for the original brief — **the kickoff is
the source of truth and wins where this file disagrees.**

Last updated: 2026-09-05, end of Phase 0.

---

## 1. The one thing not to lose

The contribution is **the retriever**, not the agent loop. Everything exists to produce one honest
number: graph-grounded retrieval vs. an embedding baseline on SWE-bench Verified, same model, same
loop, swapped by a flag. A negative result is a result — it gets reported, not buried.

Two invariants that protect that number, both already load-bearing in code:

- **`Context` is the only channel out of a retriever.** Both modes construct the same dataclass.
  `Context.mode` and `Context.stats` are for reporting; if the agent loop ever branches on them,
  the eval is measuring the branch instead of the graph.
- **A stubbed run must never be scoreable.** Every placeholder sets `stats["stub"] = True` and the
  run carries `Result.stub`. The eval harness (Phase 4) must refuse to score on that flag. Test
  `test_phase0_retrievers_flag_themselves_as_stubs` is the tripwire.

---

## 2. Where the work is

| Phase | State |
|---|---|
| 0 — scaffold, interfaces, stub CLI, tests | **done**, 4 commits |
| 1 — code graph engine | **next**, plan agreed but not written |
| 2 — Docker sandbox + SWE-bench harness | not started; **blocked on Docker daemon**, see §5 |
| 3 — LangGraph agent loop | not started |
| 4 — embedding baseline + the comparison | not started |
| 5 — README/CI/diagram polish | not started |
| 6 — MCP server (stretch) | not started |

### What is real vs. what is a placeholder

Real: `retrieval/base.py` (Context, Issue, RepoRef, Snippet, Retriever), `graph/analyzer_base.py`
(LanguageAnalyzer, SymbolDef, ImportRef, CallRef, BaseRef, FileAnalysis), `cli.py`, the tests.

Placeholder, and marked as such in its own docstring: `retrieval/stub.py` and both retrievers,
which return the head of the first *k* `.py` files; `agent/orchestrator.py`, which is straight-line
and emits `STUB_PATCH` with no model call. **`retrieval/stub.py` gets deleted once both real
retrievers land** — it exists only so the wiring could be exercised before the intelligence.

Not yet created (deliberately — empty files that pretend to exist are worse than absent ones):
`graph/python_analyzer.py`, `graph/graph_builder.py`, `graph/blast_radius.py`, `sandbox/`,
`llm/client.py`, `eval/run_eval.py`, `eval/report.py`, `mcp/server.py`.

---

## 3. Design decisions already made (don't relitigate)

- **Python 3.12, not the 3.14 on PATH.** tree-sitter, faiss and sentence-transformers all lag a
  release; finding that out mid-eval would mean rebuilding the environment with runs in flight.
  `py -0p` shows 3.14 / 3.12 / 3.10 installed; `uv venv --python 3.12` picks the right one.
- **Analyzers are file-local and syntactic.** They report what one file says about itself and never
  resolve a name across files. All cross-file resolution ("which `foo` did this call mean?") lives
  in `graph_builder`. This is what makes the TypeScript analyzer a plug-in rather than a rewrite.
- **`ImportRef.module` keeps the source spelling, dots and all**, with `level` for leading dots.
  Only the resolver knows where a file sits in the package tree, so a relative import must reach it
  intact.
- **Snippet line spans are 1-based inclusive** — matching git, tracebacks and editors, i.e. every
  tool a human cross-checks a citation against.
- **Optional dependency groups** (`agent` / `eval` / `embedding`) so the graph engine installs
  without dragging in LangGraph or Docker.
- **`results/` is committed.** Run outputs and `REPORT.md` are the deliverable; only
  `raw_trajectories/` is ignored.

---

## 4. Commands

Everything runs through `uv`. It is installed to the user site and **is not on PATH**:

```bash
export PATH="/c/Users/Kanish/AppData/Roaming/Python/Python312/Scripts:$PATH"
```

```bash
uv sync --group dev                # env
uv run pytest                      # 15 tests, ~0.2s
uv run ruff check .                # must be clean; CI lint must never --fix
uv run cartographer resolve --repo . --issue issue.txt --mode graph --k 4
uv run cartographer modes
```

Green baseline as of Phase 0: **`ruff` clean, 15 passed**.

---

## 5. Known hazards and open items

- **Docker daemon is not responding.** `docker --version` answers (29.5.3) but `docker info` hangs
  past 120s — Desktop is installed but the engine isn't up. Phase 2 cannot start until that is
  fixed. Check before planning any sandbox work.
- **`uv` is not on PATH** — see §4. A session that forgets this gets `command not found` and may
  waste time reinstalling.
- **Git Bash mangles Rich's box-drawing and `·`/`—`** when output is piped (shows as `?`). Cosmetic
  in a pipe; unverified whether a real Windows terminal renders it correctly. Don't "fix" the CLI
  over a piped screenshot.
- **CRLF warnings on every `git add`** — noise on Windows, not a problem.
- **`pyproject.toml` needs `README.md` to exist** or the hatchling build fails during `uv sync`.
  Bit me once.
- Bash heredocs are unreliable for large source files here; write source with the Write tool, or a
  small Python script doing exact string replacement with an `assert old in s` guard so a missed
  match is loud.

---

## 6. Working agreement in force

Set by the builder at session start; applies to every session:

1. **Authorship**: every commit is his. No `Co-Authored-By`, no AI attribution anywhere in a
   message, PR body or comment. Verify with
   `git log --format='%an <%ae>' | sort -u` and a grep for `co-authored|generated with|claude`.
2. **Autonomy**: make the call, do the work, report the decision. Don't present menus. The one
   standing exception is in the kickoff: *show the Phase 1 plan before writing the graph builder.*
3. **Commits**: one per coherent unit, message matches the staged diff, body explains why. Split a
   file across two commits rather than writing a vague message.
4. **Verify against reality**: probe before designing, run the real thing, hit the real dependency.
   **Mutation-check every suite** — break an assertion and confirm red. This already paid: the
   first cut of the Phase-0 tests passed with an off-by-one in `Snippet.end_line`, and the
   span/text consistency assertion was added to close it.
5. **Honesty**: say plainly what is unverified, and keep saying it. Real numbers, not rounded
   impressions.
6. **CI**: lint → typecheck → build → tests, cheapest first. CI lint must not auto-fix. A skipped
   test under `CI=true` is a hard failure.
7. **Scope**: finish the whole task; fix real defects found in passing and say so; don't add docs,
   changelogs or refactors nobody asked for.

---

## 7. Picking up: Phase 1

Plan is in §"Phase 1" of the session log / restated here in short — build in this order:

1. `graph/python_analyzer.py` on stdlib `ast` (behind `LanguageAnalyzer`, so tree-sitter can
   replace it), returning `FileAnalysis` and never raising on a syntax error.
2. `graph/graph_builder.py`: module resolution (absolute + relative), symbol table, then
   `networkx.DiGraph` with `imports` / `calls` / `inherits` edges. Own the fact that call
   resolution is *heuristic* in Python and record confidence on the edge rather than pretending.
3. `graph/blast_radius.py`: seeds → ranked subgraph. Reverse-reachability (who calls the seed)
   weighted by distance and fan-in, the Arch Lens instability idea reused.
4. `retrieval/graph_retriever.py`: seed extraction from issue text, then rank → `Snippet`s under a
   token budget. Delete `retrieval/stub.py` when the embedding retriever also lands.
5. Validate on a real mid-size Python OSS repo with a *known* past fix: does the true fix location
   appear in the top-k? Write that up as the README worked example. **This validation is the point
   of Phase 1** — an unvalidated graph is a plausible-looking artifact, not a moat.
