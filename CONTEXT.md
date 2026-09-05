# Cartographer — session handover

Working context for whoever picks this up next. Read this first, then `README.md` for the pitch and
`cartographer_kickoff.md` for the original brief — **the kickoff is the source of truth and wins
where this file disagrees.**

Repo: <https://github.com/KanishKhetarpal/cartographer> (private). Local: `~/projects/cartographer`.
Last updated: 2026-09-06, end of Phase 1 core.

---

## 1. The one thing not to lose

The contribution is **the retriever**, not the agent loop. Everything exists to produce one honest
number: graph-grounded retrieval vs. an embedding baseline on SWE-bench Verified, same model, same
loop, swapped by a flag. A negative result is a result — it gets reported, not buried.

Three invariants, all load-bearing in code:

- **`Context` is the only channel out of a retriever.** Both modes construct the same dataclass. If
  the agent loop ever branches on `mode` or `stats`, the eval measures the branch, not the graph.
- **A stubbed run must never be scoreable.** The flag is `Result.stub` (still `True` — no model is
  called yet). `Context.stats["stub"]` now marks only the *embedding* placeholder.
- **The retriever must never rank below its own seeds.** Lexical seed extraction is itself a
  retriever, and for a while the graph scored *worse* than it at small k. See §4.

---

## 2. Where the work is

| Phase | State |
|---|---|
| 0 — scaffold, interfaces, stub CLI | **done** |
| 1 — code graph engine | **core done**; README worked example + `min_confidence` ablation outstanding |
| 2 — Docker sandbox + SWE-bench harness | not started; **blocked on Docker daemon**, §6 |
| 3 — LangGraph agent loop | not started |
| 4 — embedding baseline + the comparison | not started |
| 5 — README/CI/diagram polish | not started |
| 6 — MCP server (stretch) | not started |

**Real now:** `graph/python_analyzer.py`, `graph/graph_builder.py`, `graph/blast_radius.py`,
`retrieval/seeds.py`, `retrieval/graph_retriever.py`, `eval/graph_hit_rate.py`, `cli.py`.

**Still a placeholder, and says so in its own docstring:** `retrieval/embedding_retriever.py` and
`retrieval/stub.py` (delete `stub.py` when the real baseline lands); `agent/orchestrator.py`, which
is straight-line and emits `STUB_PATCH` with no model call.

**Not created yet, deliberately** — an empty file that pretends to exist is worse than an absent
one: `sandbox/`, `llm/client.py`, `eval/run_eval.py`, `eval/report.py`, `mcp/server.py`.

---

## 3. How the pieces fit

```
issue text ──▶ seeds.py ──▶ blast_radius.py ──▶ graph_retriever.py ──▶ Context
                  │              ▲
                  └──────────────┴── graph_builder.py ◀── python_analyzer.py
```

- **Analyzer** is file-local and syntactic. It records what one file says about itself and resolves
  nothing — it has not seen the other files. A call is stored as the dotted expression the source
  wrote (`self.foo`, `json.dumps`).
- **Builder** owns every cross-file decision. Nodes are `path::qualname` (module node is
  `path::<module>`); edges are `contains` / `imports` / `calls` / `inherits` on a `MultiDiGraph`.
- **Blast radius** is a multi-source weighted walk. Callers outrank callees, mass is split by degree
  (so hubs absorb but cannot broadcast), and mass flows outward only.
- **Retriever** orders evidence-first, then graph, then cuts snippets to a token budget.

---

## 4. Measured facts — design was driven by these, don't re-derive them

**Call resolution is undecidable in Python without type inference.** Measured on flask, 3963 call
sites: only ~10% are a bare name bound by an import or local def; a third of receivers are local
variables. Hence `confidence` + `via` on every `calls` edge. Tiers: `direct` 1.0, `attribute` 0.9,
`self_mro` 0.85, `unique_name` 0.5, `ambiguous` 0.25, and **above 3 candidates no edge at all** —
`get` is called at 385 sites in flask, and noise in a blast radius is worse than a missing edge
because it silently spends the token budget on wrong files.

**Graph on flask after the resolver fixes:** 83 files, 1663 nodes, 3381 edges, 0.26s.
`direct` 343 · `attribute` 312 · `self_mro` 121 · `unique_name` 718 · `ambiguous` 268 ·
`external` 565 (correctly refused) · `too_ambiguous` 510 · `unresolved` 931.

**Phase-1 hit rate — 59 SWE-bench Verified instances** (requests 8, pytest 19, pylint 10,
xarray 22), file-level recall of the gold patch. `results/phase1_hitrate.json`:

| k | graph | seeds-only |
|---|---|---|
| 1 | 0.280 | 0.280 |
| 3 | 0.412 | 0.412 |
| 5 | 0.531 | 0.523 |
| 10 | 0.582 | 0.557 |
| 20 | **0.740** | 0.557 |

⚠️ **Read that honestly: the graph is worth +18 points at k=20 and roughly nothing below k=5.**
The thesis is about *tight* context, so this is a weaker result than the pitch wants. Closing the
small-k gap is the most valuable open work in the project.

Per repo, pylint is the outlier: ~0.13 either way, with 5 of 10 instances seeding **nothing**,
because those issues quote command-line output rather than naming code. That is a seed-extraction
gap, not a graph gap.

---

## 5. Commands

`uv` is installed to the user site and **is not on PATH**:

```bash
export PATH="/c/Users/Kanish/AppData/Roaming/Python/Python312/Scripts:$PATH"
```

```bash
uv sync --group dev
uv run pytest                 # 88 tests, ~1.3s
uv run ruff check .           # must be clean; CI lint must never --fix
uv run cartographer resolve --repo . --issue issue.txt --mode graph --k 4
uv run python eval/graph_hit_rate.py \
    ../_fixtures/swebench_verified.json ../_fixtures --repos requests --quiet
```

Green baseline: **ruff clean, 88 passed.**

Fixtures live in `~/projects/_fixtures` (untracked, outside the repo): full clones of flask,
requests, pytest, pylint, xarray, plus `swebench_verified.json` — all 500 Verified instances,
pulled from the HF datasets-server with no auth. ⚠️ The eval **checks those clones out at each
instance's `base_commit`** and leaves them on a detached head. They are throwaway; don't work in
them.

---

## 6. Known hazards

- **Docker daemon is not responding.** `docker --version` answers (29.5.3) but `docker info` hangs
  past 120s. Phase 2 cannot start until Desktop is up.
- **`uv` is not on PATH** — §5. A session that forgets this wastes time reinstalling it.
- **Bare `python` is 3.14, not the project's 3.12**, and it reads `/tmp/x` as a Windows-relative
  path. Use `uv run python`, or `/c/Python312/python.exe` for one-off scripts.
- ⚠️ **Shell heredocs collapse backslash escapes here.** This wrote a regex containing a literal
  0x08 byte where `\b` was intended: it compiled, read correctly in the source, and could never
  match. The only symptom was "0 seeds". Write escape-heavy source with the Write tool, or build
  the string with `chr(92)`. `tests/test_seeds.py` has a regression test; a repo-wide sweep found
  no other control bytes.
- **`pyproject.toml` needs `README.md` to exist** or hatchling fails during `uv sync`.
- Rich's box-drawing renders as `?` through a Git Bash pipe. Cosmetic; unverified in a real
  terminal. Don't "fix" the CLI over a piped screenshot.
- CRLF warnings on every `git add` are Windows noise.

---

## 7. Working agreement in force

1. **Authorship**: every commit is his. No `Co-Authored-By`, no AI attribution anywhere. Verify:
   `git log --format='%an <%ae>' | sort -u` and grep for `co-authored|generated with|claude`.
2. **Autonomy**: make the call, do the work, report the decision. The one standing exception is in
   the kickoff: *show the Phase 1 plan before writing the graph builder* (done).
3. **Commits**: one per coherent unit; message matches the staged diff; body explains why.
4. **Verify against reality**: probe before designing, run the real thing. **Mutation-check every
   suite.** This has paid four times now — see §8.
5. **Honesty**: say what is unverified and keep saying it. Real numbers.
6. **CI**: lint → typecheck → build → tests. CI lint must not auto-fix. A skipped test under
   `CI=true` is a hard failure.
7. **Scope**: finish the whole task; fix real defects found in passing and say so.

---

## 8. What mutation testing has caught so far

Kept because it is the strongest argument for continuing to do it:

- An off-by-one in `Snippet.end_line` — all 15 Phase-0 tests passed with it.
- Both re-export tests passed with **re-export resolution disabled**: the short-name fallback
  reached the same node by a weaker route. They assert `via` and confidence now, not just the node.
- The decay test held with decay switched off (degree-splitting alone explained the result).
- Seed pinning held only because the seed happened to score highest.

Two of my own mutations were also invalid (`set() or {...}` is a no-op; a test filtered
`pkg/util.py` as if it were `pkg/u<N>.py`). When a mutation survives, check the mutation before
trusting the test.

---

## 9. Next actions, in order

1. **Close the small-k gap** — the headline weakness. Two concrete leads: pylint-style issues that
   seed nothing (parse command-line output and error codes), and re-ranking so a
   high-confidence-only subgraph decides the top 3.
2. **Run the `min_confidence` ablation.** The plumbing exists and is tested but has never been
   swept. It answers "does the guessed half of the graph help or hurt?" with a number — that is a
   README-grade finding either way.
3. **README worked example** — Phase 1 is not closed without it (kickoff §Phase 1).
4. Widen the eval to django/sympy for a larger n before drawing conclusions from 59 instances.
5. Then Phase 2, once Docker is up.
