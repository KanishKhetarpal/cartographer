# PROJECT: Cartographer — a graph-grounded autonomous code agent

> Paste this whole file into Claude Code as the opening prompt. Treat it as the project's source of truth. Read it fully, then start at **PHASE 0** and keep going. Ask me only when a decision is genuinely irreversible or ambiguous — otherwise make the reasonable call, note it, and proceed.

---

## 1. Mission (one sentence)

Build an autonomous agent that takes a real GitHub issue, selects the **minimal relevant context using a program dependency graph** (not brute-force file dumps), plans and writes a fix, runs the repo's real tests in a sandbox, iterates until green, and opens a PR — and **prove** the graph-grounding beats a diff/retrieval-only baseline on **SWE-bench Verified**.

## 2. Why this exists (the thesis — do not lose this)

Everyone can wire an LLM into a "fix my code" loop. They all fail the same way: on real repos they either stuff whole files into the prompt (context blows up, accuracy craters) or do naive embedding top-k retrieval (misses the call sites that actually break). **The novel contribution here is the context retriever**: a dependency/call graph that, given an issue, returns the ranked *blast-radius* subgraph — the functions and files a correct fix must touch. The agent reasons over that tight, structurally-correct context.

The headline result we are chasing:
> "Grounding the agent's context in a program dependency graph lifts SWE-bench Verified resolved-rate by **N points** over an embedding-retrieval baseline using the same model and same agent loop."

Everything we build serves producing that number honestly. **The graph is the moat. The agent loop is table stakes.**

## 3. Definition of done (v1 — the résumé-grade milestone)

1. Runs end-to-end on a SWE-bench Verified task: issue in → patch out → tests run in Docker → resolved/unresolved verdict.
2. Two retrieval modes behind one interface: `graph` (ours) and `embedding` (baseline). Same model, same loop, swappable by a flag.
3. An eval run over a fixed **≥50-task** SWE-bench Verified subset produces a comparison table: resolved-rate, avg files touched, avg tokens/attempt, cost/task — `graph` vs `embedding`.
4. A `results/` folder with the raw run logs and a generated `REPORT.md` table. Reproducible with one command.
5. A README that opens with a demo GIF/asciinema + the results table, then architecture.
6. **Stretch (v1.1):** expose the whole thing as an MCP server so Claude Code / Cursor can call `resolve_issue` and `blast_radius` as tools.

If the graph does **not** beat the baseline, that is a finding, not a failure — report it honestly and dig into *why* (which task categories it wins/loses). A rigorous negative result still demonstrates the exact skills the market is paying for.

## 4. Architecture

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
                │        CODE GRAPH ENGINE (the moat)            │
                │  tree-sitter parse → symbol table →            │
                │  import + call + inheritance graph →           │
                │  blast-radius ranking (fan-in/out, distance)   │
                └────────────────────────────────────────────────┘
```

**Components**

- **Code Graph Engine** — parses a repo into a directed graph of files → symbols (functions/classes/methods) with edges for imports, calls, and inheritance. Given a set of seed symbols/files (extracted from the issue text + failing test), it returns the ranked **blast-radius subgraph**: the k nearest impacted nodes by graph distance, weighted by fan-in/fan-out (reuse the instability idea from Arch Lens). Language-pluggable via tree-sitter; **Python analyzer first** (SWE-bench is Python), TS analyzer (port Arch Lens' ts-morph logic) as the second plug-in.
- **Retriever (interface)** — `retrieve(issue, repo) -> Context`. Two implementations: `GraphRetriever` (ours) and `EmbeddingRetriever` (baseline: chunk repo, embed, top-k). Identical output shape so the loop can't tell them apart.
- **Orchestrator** — LangGraph state machine: `plan → retrieve → edit → run_tests → reflect`, looping on failure up to a budget (e.g. 4 iterations), then emit patch or give up. Log every state transition.
- **Code Editor** — applies the model's proposed edits as a unified diff / structured patch; rejects patches that don't apply cleanly and feeds the error back.
- **Sandbox** — Docker per task; installs the repo at the task's base commit and runs the specified FAIL_TO_PASS / PASS_TO_PASS tests. Use the official SWE-bench evaluation harness for scoring — do not hand-roll verdict logic.
- **Eval Harness** — runs a task subset in both modes, collects metrics, writes `results/` + `REPORT.md`.
- **MCP server (stretch)** — FastAPI/MCP wrapping `blast_radius(repo, issue)` and `resolve_issue(repo, issue)`.

## 5. Tech stack (pinned decisions — don't relitigate)

- **Language:** Python 3.11+. `uv` for env/deps.
- **Agent framework:** LangGraph (state machine + checkpointing). LangChain only where it saves real work.
- **Parsing / graph:** `tree-sitter` + `tree-sitter-python` (+ grammars later); `networkx` for the graph. Python stdlib `ast` acceptable for the Python analyzer if simpler than tree-sitter for v0 — but keep the analyzer behind an interface so tree-sitter can replace it.
- **LLM client:** model-agnostic wrapper (Anthropic + OpenAI); config-driven model id. Default to a strong coding model; make it a single config value so cost/model can be swapped for eval fairness.
- **Sandbox / eval:** Docker + the official **SWE-bench** harness (`swebench` package / datasets `princeton-nlp/SWE-bench_Verified`).
- **Embeddings baseline:** any standard embedding model + FAISS or pgvector. Keep it deliberately vanilla — it's the control, not a competitor.
- **Serving (stretch):** FastAPI + the MCP Python SDK.
- **Eval infra:** structured JSON logs; a small `REPORT.md` generator. Wire an eval gate into CI later.

## 6. Repository structure

```
cartographer/
├── README.md
├── pyproject.toml            # uv-managed
├── cartographer/
│   ├── graph/                # THE MOAT
│   │   ├── analyzer_base.py  # LanguageAnalyzer interface
│   │   ├── python_analyzer.py
│   │   ├── graph_builder.py  # symbols + import/call/inherit edges
│   │   └── blast_radius.py   # seed → ranked impacted subgraph
│   ├── retrieval/
│   │   ├── base.py           # Retriever interface + Context dataclass
│   │   ├── graph_retriever.py
│   │   └── embedding_retriever.py   # baseline
│   ├── agent/
│   │   ├── orchestrator.py   # LangGraph graph
│   │   ├── nodes.py          # plan/edit/test/reflect nodes
│   │   └── prompts.py
│   ├── sandbox/
│   │   └── docker_runner.py
│   ├── llm/
│   │   └── client.py         # model-agnostic
│   └── mcp/                  # stretch
│       └── server.py
├── eval/
│   ├── run_eval.py           # subset × {graph,embedding} → results/
│   ├── tasks_subset.json     # the fixed ≥50-task list
│   └── report.py             # results/ → REPORT.md
├── results/                  # committed run outputs + REPORT.md
└── tests/
```

## 7. Build phases — work through these in order

**PHASE 0 — Scaffold (start here, now)**
- Create the repo structure above, `pyproject.toml` with `uv`, a working `LanguageAnalyzer`/`Retriever`/`Context` interface skeleton, and a stub CLI: `cartographer resolve --repo <path> --issue <file> --mode graph`. It should run end-to-end with stubbed internals and print a fake patch. Get the wiring right before the intelligence.
- Add `tests/` with a couple of interface tests. Commit.

**PHASE 1 — Code Graph Engine (the moat — spend the most care here)**
- Python analyzer: parse a repo into files → functions/classes; build import, call, and inheritance edges in `networkx`.
- `blast_radius(seeds, k)`: from seed nodes, return the ranked impacted subgraph (graph distance + fan-in/fan-out weighting).
- Validate on a real mid-size Python OSS repo: pick a known past bug, seed with the file/symbol names in the issue, confirm the true fix location is in the top-k. Write this up in the README as a worked example.

**PHASE 2 — Sandbox + SWE-bench wiring**
- Integrate the SWE-bench Verified dataset; stand up the Docker runner; score ONE task end-to-end with a hardcoded/known patch to confirm the harness verdict pipeline works before adding the agent.

**PHASE 3 — Agent loop**
- LangGraph `plan → retrieve → edit → run_tests → reflect`, iteration budget, full transition logging. Get it resolving at least a handful of tasks in `graph` mode.

**PHASE 4 — Baseline + the comparison (the payoff)**
- Implement `EmbeddingRetriever`. Run the ≥50-task subset in both modes. Generate `REPORT.md` with the comparison table. This table is the whole point — make it clean and honest.

**PHASE 5 — Polish for the world**
- README: demo GIF/asciinema first, then results table, then architecture diagram (generate it — this is literally what Arch Lens does), then a "Design & tradeoffs" section (why the graph, what changes at 10× scale).
- Green CI badge; eval smoke-test in GitHub Actions.

**PHASE 6 — Stretch: MCP server**
- Wrap `blast_radius` and `resolve_issue` as MCP tools so it plugs into Claude Code / Cursor. Record a 90-sec demo of it closing a real issue on one of my own repos.

## 8. Guardrails — things NOT to do

- Don't skip the baseline. A win with nothing to compare against is worthless.
- Don't hand-roll the SWE-bench verdict — use the official harness so the number is defensible.
- Don't let the retriever dump whole files "to be safe." The tight-context discipline IS the contribution; measure tokens/attempt and keep it low.
- Don't over-engineer multi-language support in v1. Python-first, clean interfaces, TS later.
- Don't fake or hand-pick tasks to inflate the number. Fixed subset, committed task list, reproducible.
- Keep every model id, k, and iteration budget in one config so eval comparisons stay fair.

## 9. Context about me (the builder) — use this to make choices

- I built **Arch Lens** (github.com/KanishKhetarpal/arch-lens): TypeScript AST parsing with ts-morph, dependency graphs, Tarjan's SCC for cycle detection, fan-in/fan-out instability metrics, auto architecture diagrams. **Reuse these ideas directly** — the blast-radius ranking is a close cousin of what Arch Lens already computes, and the Phase-5 architecture diagram can be generated by Arch Lens itself.
- I built **RippleReview**: fused TS AST static analysis with an LLM for change blast-radius review, with precision/recall evals wired into CI. Cartographer is the agentic evolution of that idea — carry over the eval rigor.
- I'm strongest in TypeScript/Node and Java/Spring; comfortable in Python. Explain any non-obvious Python-ecosystem choices briefly as you go.

---

**Begin with PHASE 0 now.** Scaffold the repo, get the stub CLI running end-to-end, commit, then move to PHASE 1 and start building the Code Graph Engine. Show me the plan for Phase 1 before you write the graph builder.
