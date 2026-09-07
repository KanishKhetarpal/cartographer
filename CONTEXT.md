# Cartographer — session handover

Working context for whoever picks this up next. Read this first, then `README.md` for the pitch and
`cartographer_kickoff.md` for the original brief — **the kickoff is the source of truth and wins
where this file disagrees.**

Repo: <https://github.com/KanishKhetarpal/cartographer> (private). Local: `~/projects/cartographer`.
Last updated: 2026-09-07.

---

## 1. The one thing not to lose

The contribution is **the retriever**, not the agent loop. Everything exists to produce one honest
number: graph-grounded retrieval vs. an embedding baseline on SWE-bench Verified, same model, same
loop, swapped by a flag. A negative result is a result — it gets reported, not buried.

Four invariants, all load-bearing in code:

- **`Context` is the only channel out of a retriever.** Both modes construct the same dataclass. If
  the agent loop ever branches on `mode` or `stats`, the eval measures the branch, not the graph.
- **A stubbed run must never be scoreable.** The flag is `Result.stub` (still `True` — no model is
  called yet). `Context.stats["stub"]` now marks only the *embedding* placeholder.
- **The retriever must never rank below its own seeds.** Lexical seed extraction is itself a
  retriever, and the graph once scored *worse* than it at small k. Hence evidence-first ordering.
- **The eval calls the shipped retriever**, never its own copy of the ranking. A copy drifts, and
  the drift always flatters the eval.

---

## 2. Where the work is

| Phase | State |
|---|---|
| 0 — scaffold, interfaces, stub CLI | **done** |
| 1 — code graph engine | **done**: analyzer, resolver, blast radius, retriever, validation, worked example, ablation |
| 2 — Docker sandbox + SWE-bench harness | not started; **blocked on Docker daemon**, §6 |
| 3 — LangGraph agent loop | not started; **needs a spending decision**, §7 |
| 4 — embedding baseline + the comparison | not started |
| 5 — README/CI/diagram polish | CI done; README has results + worked example; diagram outstanding |
| 6 — MCP server (stretch) | not started |

**Real now:** `graph/python_analyzer.py`, `graph/graph_builder.py`, `graph/blast_radius.py`,
`retrieval/seeds.py`, `retrieval/graph_retriever.py`, `cli.py`, and in `eval/`:
`graph_hit_rate.py`, `ablation.py`, `worked_example.py`, `report.py`.

**Still a placeholder, and says so in its own docstring:** `retrieval/embedding_retriever.py` and
`retrieval/stub.py` (delete `stub.py` when the real baseline lands); `agent/orchestrator.py`, which
is straight-line and emits `STUB_PATCH` with no model call.

**Not created yet, deliberately** — an empty file that pretends to exist is worse than an absent
one: `sandbox/`, `llm/client.py`, `eval/run_eval.py`, `mcp/server.py`.

CI: `.github/workflows/ci.yml` — lint (`--no-fix`), mypy, build, tests, then a guard that reads the
junit report and **fails on fewer than 80 tests or any skip under `CI=true`**. Green at HEAD with
105 tests, 0 skipped.

---

## 3. How the pieces fit

```
issue text ──▶ seeds.py ──▶ blast_radius.py ──▶ graph_retriever.py ──▶ Context
                  │              ▲
                  └──────────────┴── graph_builder.py ◀── python_analyzer.py
```

- **Analyzer** is file-local and syntactic; resolves nothing. Never raises — a syntax error *or* an
  AST too deep to walk is reported in `FileAnalysis.errors` so one file cannot abort a scan.
- **Builder** owns every cross-file decision. Nodes are `path::qualname` (module node is
  `path::<module>`); edges `contains` / `imports` / `calls` / `inherits` on a `MultiDiGraph`, and
  every `calls` edge carries `confidence` + `via`.
- **Blast radius** — multi-source weighted walk. Callers outrank callees, mass is split by degree
  (hubs absorb but cannot broadcast), mass flows outward only.
- **Retriever** — evidence-first ordering, then graph, then snippets under a token budget.

---

## 4. Measured facts — the design came from these, don't re-derive them

**Call resolution is undecidable in Python without type inference.** On flask, 3963 call sites: only
~10% are a bare name bound by an import or local def. Tiers: `direct` 1.0, `attribute` 0.9,
`self_mro` 0.85, `unique_name` 0.5, `ambiguous` 0.25, and **above 3 candidates no edge at all** —
`get` is called at 385 sites in flask, and noise in a blast radius silently spends the token budget
on wrong files.

**Retrieval, 59 instances** (requests 8, pytest 19, pylint 10, xarray 22) —
`results/phase1_hitrate.json`, rendered in `results/REPORT.md`:

| k | graph | seeds-only | delta |
|---|---|---|---|
| 1 | 0.356 | 0.356 | 0.000 |
| 3 | 0.556 | 0.556 | 0.000 |
| 5 | 0.616 | 0.599 | +0.017 |
| 10 | 0.684 | 0.633 | +0.051 |
| 20 | 0.774 | 0.633 | +0.141 |

⚠️ **The graph contributes nothing at k≤3** — there the issue's own text is the whole answer — and
+5 to +14 points from k=5 out. The thesis is about *tight* context, so this remains the weakest
part of the story.

**Ablation, same 59** (`results/phase1_ablation.json`): dropping every name-matched call edge
(`min_confidence` ≥ 0.60) *improves* recall at k=10, 0.684 → 0.709, and costs 0.008 at k=20. Hops
saturate by 3 (a fourth buys 0.008). ⚠️ **One instance is worth 1.7 points at n=59, so the
confidence finding is ~1.5 instances — a lead, not a result.** The default is unchanged pending a
wider run.

**Worked example** — `pytest-dev__pytest-7236`, in the README: the issue names no file at all, so
lexical retrieval scores 0.00 at every k; the graph reaches the gold file at rank 9 of 217 through
one call edge from `outcomes.py::skip`, in ~604 tokens. Regenerate with `eval/worked_example.py`.

**Build cost:** flask 0.26s / 83 files · pytest 1.8s / 217 · sympy 18s / 1606 · django **36s** /
2929 files, 46554 nodes, 133314 edges.

---

## 5. Commands

`uv` is installed to the user site and **is not on PATH**:

```bash
export PATH="/c/Users/Kanish/AppData/Roaming/Python/Python312/Scripts:$PATH"
```

```bash
uv sync --group dev
uv run pytest                       # 105 tests
uv run ruff check .                 # must be clean; CI lint never --fix
uv run --with mypy mypy cartographer --ignore-missing-imports
uv run cartographer resolve --repo . --issue issue.txt --mode graph --k 6

F=../_fixtures; D=$F/swebench_verified.json
uv run python eval/graph_hit_rate.py  $D $F --repos requests --quiet
uv run python eval/ablation.py        $D $F --repos sympy --max-per-repo 20
uv run python eval/worked_example.py  $D $F pytest-dev__pytest-7236
uv run python eval/report.py          # -> results/REPORT.md
```

Fixtures in `~/projects/_fixtures` (untracked): clones of flask, requests, pytest, pylint, xarray,
sympy, django, plus `swebench_verified.json` — all 500 Verified instances, pulled from the HF
datasets-server with no auth.

⚠️ **The evals check those clones out at each instance's `base_commit`** and leave them on a
detached head. They are throwaway; don't work in them. **Only one eval at a time** — a
`FixtureLock` in `eval/graph_hit_rate.py` enforces it (see §6).

---

## 6. Known hazards

- **Docker daemon is not responding.** `docker --version` answers 29.5.3 but `docker version`
  times out (exit 124). Phase 2 cannot start until Desktop is up. Re-checked 2026-09-07.
- **`uv` is not on PATH** — §5.
- **Bare `python` is 3.14, not the project's 3.12**, and it reads `/tmp/x` as a Windows-relative
  path. Use `uv run python`, or `/c/Python312/python.exe` for one-off scripts.
- ⚠️⚠️ **Shell heredocs collapse backslash escapes here, repeatedly.** This has now caused three
  separate defects: a regex containing a literal `0x08` byte where `\b` was meant (compiled, read
  correctly, could never match — symptom was "0 seeds"); a broken test asserting the same thing;
  and a whole test file with `\n` turned into real newlines inside string literals. **Write
  escape-heavy source with the Write tool.** If you must go through the shell, build the string
  with `chr(92)` / `chr(10)` and no escapes at all.
- ⚠️ **Two evals at once silently corrupt each other** — they drive the same clones. One moves HEAD
  while the other parses, checkouts fail, instances are skipped rather than scored, and a
  10-instance repo reports 1. It reads as a code regression. `FixtureLock` now makes it a crash;
  if a run dies hard, delete `_fixtures/.cartographer-eval.lock`.
- **`pyproject.toml` needs `README.md`** or hatchling fails during `uv sync`.
- **`Path.read_text()` defaults to cp1252 here** and dies on the README's em-dashes. Always pass
  `encoding="utf-8"`. A commit once landed claiming a README change that had silently failed.
- Rich's box-drawing renders as `?` through a Git Bash pipe. Cosmetic; don't "fix" the CLI over a
  piped screenshot.

---

## 7. Decisions waiting on the owner

- **Phase 3 needs a model and therefore money.** Building the LLM client is free; verifying it
  against reality is not, and the working agreement's "run the thing" rule means it must actually
  call. No API key has been used and no spend has been incurred. Needs an explicit go-ahead, a
  provider and a budget.
- **Repo is private.** `gh repo edit --visibility public` when the Phase-5 polish is ready.

---

## 8. Working agreement in force

1. **Authorship**: every commit is his. No `Co-Authored-By`, no AI attribution. Verify with
   `git log --format='%an <%ae>' | sort -u` and grep for `co-authored|generated with|claude`.
2. **Autonomy**: make the call, do the work, report the decision.
3. **Commits**: one per coherent unit; message matches the staged diff; body explains why.
4. **Verify against reality**: probe before designing, run the real thing, **mutation-check every
   suite** — see §9.
5. **Honesty**: say what is unverified and keep saying it. Real numbers.
6. **CI**: lint → typecheck → build → tests. Never auto-fix. A skip under `CI=true` is a failure.
7. **Scope**: finish the whole task; fix real defects found in passing and say so.

---

## 9. What mutation testing has caught

The strongest argument for continuing to do it:

- An off-by-one in `Snippet.end_line` — all 15 Phase-0 tests passed with it.
- Both re-export tests passed with **re-export resolution disabled**: the short-name fallback
  reached the same node by a weaker route.
- The decay test held with decay switched off; seed pinning held only because the seed scored
  highest anyway.
- Three of the first `GraphRetriever` tests passed with the rule they name deleted, all because the
  fixture was too small for the rule to bind. They now carry explicit
  `"test is vacuous unless ..."` assertions.

Two of my own mutations were invalid (`set() or {...}` is a no-op; a `pkg/u` prefix filter also
matched `pkg/util.py`), and one mutated the parse guard while I believed it was mutating the walk
guard. **When a mutation survives, check the mutation before trusting the test.**

---

## 10. Next actions, in order

1. **Close the small-k gap** — still the headline weakness (nothing gained at k≤3).
   - pylint is the worst repo (~0.23) because its issues name **CLI flags and message codes**
     (`--ignore-paths`, `W0611`), not identifiers. Those strings live verbatim in the source that
     implements them, so a literal index would seed them. **Probe first**
     (`probe_literals.py` sketch exists in `%TEMP%`): does the string occur in the gold file, and
     in how many others?
   - Consider file-diversity in snippet selection: an issue naming several symbols in one file
     currently consumes all of `k` before the graph contributes anything.
2. **Settle the `min_confidence` finding** on the wider set, then change the default or drop it.
3. **Architecture diagram** for the README (Phase 5) — Arch Lens can generate it.
4. **Phase 2** once Docker is up; **Phase 3** once the spending decision is made (§7).
