# Cartographer — session handover

Working context for whoever picks this up next. Read this first, then `README.md` for the pitch and
`cartographer_kickoff.md` for the original brief — **the kickoff is the source of truth and wins
where this file disagrees.**

Repo: <https://github.com/KanishKhetarpal/cartographer> (private). Local: `~/projects/cartographer`.
Last updated: 2026-09-10.

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
| 2 — Docker sandbox + SWE-bench harness | **done**: harness wrapper, verified against real Docker on two instances |
| 3 — LangGraph agent loop | not started; **needs a spending decision**, §7 |
| 4 — embedding baseline + the comparison | **retrieval-side done** 2026-09-10: real `EmbeddingRetriever`, `embedding_hit_rate.py`, and the full graph-vs-embedding run on the same 59 instances Phase 1 used — see §4. The resolved-rate half still needs Phase 3. |
| 5 — README/CI/diagram polish | CI done; README has results, worked example and a self-generated diagram; demo GIF outstanding |
| 6 — MCP server (stretch) | not started |

**Real now:** `graph/python_analyzer.py`, `graph/graph_builder.py`, `graph/blast_radius.py`,
`retrieval/seeds.py`, `retrieval/graph_retriever.py`, `retrieval/embedding_retriever.py`,
`graph/render.py`, `cli.py`, `sandbox/docker_runner.py`, `sandbox/_win_launcher.py`, and in `eval/`:
`graph_hit_rate.py`, `embedding_hit_rate.py`, `ablation.py`, `worked_example.py`, `report.py`.

**Still a placeholder, and says so in its own docstring:** `agent/orchestrator.py`, which is
straight-line and emits `STUB_PATCH` with no model call. Scoring `STUB_PATCH` through the sandbox
would report 0/N resolved on every instance — correctly, since it's a no-op diff against a file
named `PLACEHOLDER` — but that number means nothing until Phase 3 produces real patches.
(`retrieval/stub.py` is gone — deleted once `EmbeddingRetriever` stopped needing a placeholder.)

**Not created yet, deliberately** — an empty file that pretends to exist is worse than an absent
one: `llm/client.py`, `eval/run_eval.py` (the ≥50-task sweep — waits on Phase 3 for real patches
and Phase 4 for the baseline to compare against), `mcp/server.py`.

CI: `.github/workflows/ci.yml` — lint (`--no-fix`), mypy, build, tests, then a guard that reads the
junit report and **fails on fewer than 80 tests or any skip under `CI=true`**. Green at HEAD with
154 tests, 0 skipped.

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

**Ablation — run twice, and the headline finding did NOT replicate.**

On the four small repos (n=59, `results/phase1_ablation.json`) dropping every name-matched call
edge (`min_confidence` ≥ 0.60) *improved* recall at k=10, 0.684 → 0.709. On the two large repos
(n=60, `results/phase1_ablation_large.json`) it does nothing at k=10 and *costs* at k=5 and k=20;
baseline ties or beats every restricted configuration there.

⚠️ **Treat the small-repo result as noise.** At n≈60 one instance is worth ~1.7 points, so a
2.5-point delta was ~1.5 instances. Not changing the default on it was correct, and keeping the
guessed edges is now an evidenced decision rather than an unexamined one. **Do not re-open this
without a much larger n.**

What holds on both sets: **1 hop is not enough, and 3 is not better than 2**; `imports-only`
(≥0.86, which also discards `self.method()` MRO resolution) is worse than baseline on large repos
at every k above 5.

**Per repo at k=20, graph vs. seeds-only:** xarray 1.000/0.886 · requests 0.875/0.625 ·
sympy 0.833/0.796 · pytest 0.737/0.553 · django 0.719/0.603 · **pylint 0.267/0.233**.

⚠️ **pylint fails on seeding, not on traversal.** Median 2 seeds per issue against xarray's 9,
because its issues quote CLI flags and message codes rather than naming code. The split is total:
all seven instances with ≤3 seeds scored 0.00 at every k; all three with ≥8 seeds scored 0.67-1.00.

**"Nothing gained at k≤3" is mostly structural, not a quality miss — probed, not assumed.**
`eval/probe_small_k_structure.py`, all 59 instances: per-instance divergence between graph and
seeds-only is 0/59 at k=1, 0/59 at k=3, 2/59 at k=5, 4/59 at k=10, 11/59 at k=20 (not just tied in
aggregate — literally the same files, per instance, at k≤3). Evidence-first ordering pins every
seed file ahead of anything inferred, and blast_radius gives seed nodes the highest injected
weight, so the graph's own top-ranked files are typically seed-associated too — the same set
seeds-only already ranks first, by a different route. Measuring the rank of the first file in the
graph's ranking that is **not** in that evidence set at all (the first position where graph and
seeds-only could even possibly differ): median **rank 6** across the 59 (p25=4, p75=8); only 11/59
(19%) have one within the top 3; 9/59 never show one in the top 20 at all.

⚠️ **This reframes next-actions #1 rather than closes it.** The graph is not failing to add value
at k≤3 — it structurally cannot show any there, for most instances, without deliberately ranking a
less-confident inferred file above evidence the issue itself provided, which is the exact trade
the never-worse-than-its-own-seeds invariant (§1) was built to refuse. The honest framing is: the
graph's real contribution starts around median rank 6, which is exactly what k=5/k=10/k=20 already
measure and report. "Nothing gained at k≤3" is a true statement about where the evidence pool ends,
not a bug in the graph's ranking within it.

**Embedding baseline, same 59 instances as the retrieval table** —
`results/phase4_hitrate.json`, real `EmbeddingRetriever` (all-MiniLM-L6-v2, fixed 40-line chunks,
cosine top-k, no function/class awareness):

| k | graph | embedding | delta |
|---|---|---|---|
| 1 | 0.356 | 0.144 | +0.212 |
| 3 | 0.556 | 0.441 | +0.116 |
| 5 | 0.616 | 0.572 | +0.044 |
| 10 | 0.684 | 0.708 | -0.024 |
| 20 | 0.774 | 0.750 | +0.024 |

⚠️ **Not a clean win, and say so.** Graph leads clearly at k=1/k=3 — where the thesis says it
should matter *least*, since the issue text alone should already answer it there — worth 7.5 and
~4 instances respectively, well above the ~1.7-point noise floor at n=59. The lead shrinks through
k=5, and **at k=10 embedding edges ahead**, before graph retakes a k=20 lead that is itself
noise-sized (≤1.5 instances either way). Read the k=1/k=3 result as real; read k=10/k=20 as "no
finding" until n is larger — same discipline as the ablation replication note above.
This is retrieval only — no patch has been scored — and rendered in `results/REPORT.md` via
`eval/report.py`.

**File-diversity in snippet selection — the other half of next-actions #1, and this one was
real.** `eval/probe_snippet_diversity.py`, all 59 instances: every hit-rate number above measures
`.rank()`, the *unbounded* node order collapsed to files — deliberately, so ranking quality isn't
confounded with k. But `.retrieve()` (what an agent actually gets) stops at k *snippets* (default
12), and snippets are symbol-level, so several can share one file. Compared, at the same k=12,
what `.retrieve()` actually delivers against what the unbounded ranking says should be reachable:

| | recall | instances losing gold to it |
|---|---|---|
| before the fix | 0.633 | 5 / 59 |
| after the fix | 0.692 | 0 / 59 |

The gap was real: a file the issue names heavily could occupy several of the top-ranked slots and
spend all of k before a second file was considered at all, even when the unbounded ranking had
already reached the gold file within the same k. Fixed in `graph_retriever.py`'s `retrieve()` —
a file-diversity pass takes each file's best-ranked node first, in rank order, before any file
gets a second node — closes the gap completely (delivered recall now equals the unbounded
ranking's, exactly, on these 59). Does **not** change `.rank()` or any number reported above; the
retrieval-vs-baseline comparisons are unaffected because they were never measuring this. Mutation
tested: `test_file_diversity_reaches_a_second_file_within_k` fails without the fix
(`tests/test_graph_retriever.py`).

**Literal seeding, probed against all 10 pylint instances** — `eval/probe_literal_seeding.py`:
extract CLI flags (`--ignore-paths`) and message codes (`W0611`) from the issue text, and check
whether the string occurs in the gold file and in ≤8 files total (any more and it is not a useful
seed regardless of whether one of those files is gold).

⚠️ **The premise mostly does not hold. Only 2 of 10 instances would be reached.** 5 instances have
no candidate literal in the issue text at all; 3 more have selective candidates (≤8 files) that
never land on gold — e.g. pylint-7080 has 11 useful candidates and none is the gold file. Literal
seeding is not the broad fix the framing in next-actions #1 (below, now struck through) implied.
It would help a real but small slice of pylint, not close the gap.

**Worked example** — `pytest-dev__pytest-7236`, in the README: the issue names no file at all, so
lexical retrieval scores 0.00 at every k; the graph reaches the gold file at rank 9 of 217 through
one call edge from `outcomes.py::skip`, in ~604 tokens. Regenerate with `eval/worked_example.py`.

**Build cost:** flask 0.26s / 83 files · pytest 1.8s / 217 · sympy 18s / 1606 · django 13.5s warm
(36s cold) / 2929 files, 46554 nodes, 133314 edges.

⚠️ **An eval instance on django costs ~75s end to end, not 14s.** Build is 13.5s warm and all seven
ablation rankings together are 0.7s; the rest is `git checkout` across 2929 files, which also
evicts the page cache the next build wants. Budget by checkout cost, not by build cost -- I
launched a 179-instance run estimating 25 minutes and it was on track for nearly four hours.

---

## 5. Commands

`uv` is installed to the user site and **is not on PATH**:

```bash
export PATH="/c/Users/Kanish/AppData/Roaming/Python/Python312/Scripts:$PATH"
```

```bash
uv sync --group dev
uv run pytest                       # 154 tests
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

- **The real SWE-bench harness corrupts its own `eval.sh` on native Windows Python** —
  `Path.write_text` with no `newline=""` turns every `\n` into `\r\n`; the container then reads
  `cd /testbed\r` and fails every command in it, cascading through `CondaError` → missing
  `pytest` → a genuine gold patch scoring **unresolved**. Fixed in `_win_launcher.py`
  (monkeypatches the default before `swebench` is imported); `docker_runner.run()` always goes
  through it, never through `-m swebench.harness.run_evaluation` directly. Confirmed fixed against
  real Docker on two instances. **If a future swebench version, or any other subprocess, writes a
  script for a Linux container from Windows Python, expect this again.**
- **Docker daemon was down through 2026-09-07; up as of 2026-09-09.** No guarantee it stays up —
  check with `docker info` (not `docker version`, see next line) before assuming Phase 2/3 work is
  runnable.
- ⚠️ **`docker version` can time out (exit 124) even when the daemon is genuinely up.** Confirmed
  2026-09-09: `docker info` answered instantly (`os=linux driver=overlayfs`) in the same session
  where `docker version` had hung minutes earlier. **Use `docker info` to check the daemon, not
  `docker version`.**
- **`swebench`'s dataset row schema changed since the kickoff was written.** Version 5.0.2 (what
  `pip` resolves for `>=3.0`) no longer builds an instance's Docker image from a spec — every row
  of `SWE-bench/SWE-bench_Verified` on Hugging Face carries a pre-built `image` field pulled from
  Docker Hub instead. Our local `_fixtures/swebench_verified.json` predates this and has no
  `image` key (`KeyError: 'image'` if you try). It stays correct for Phase 1, which never needed
  that field; `sandbox/docker_runner.py` reads the HF dataset directly for scoring, never the
  fixture.
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

## 7a. Cost note for whoever runs Phase 4's real sweep

Each real scored instance costs real time: scikit-learn-14141 took ~20s once the image was
cached, ~75s cold (image pull); django-16082 took ~7s test runtime but a longer cold pull (django
images run larger). **Pull time dominates on a fresh machine, not test time.** A ≥50-task sweep
across many different repos means many different multi-GB images — budget disk (each image is
GBs; `docker system df` before a big run) and wall-clock by pulls, not by the ~10-20s test runs
the log will show once things are warm. This is the same lesson §4's `git checkout` cost note
teaches for Phase 1's ablations, one layer up.

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

1. ~~Close the small-k gap~~ — **reframed 2026-09-12, not "fixed" because it mostly isn't a
   bug.** See §4: median rank 6 for the graph's first genuinely-inferred file across the 59
   instances, so k≤3 is mostly structural (evidence-first + seed-weighted blast_radius), not a
   quality miss. Don't re-open this as "make k≤3 better" without first deciding whether to trade
   away the never-worse-than-seeds invariant — that's the actual lever, and it's a real design
   trade-off, not a bug fix. What remains legitimately open:
   - ~~Probe whether literal seeding (CLI flags, message codes) reaches pylint's gold files~~ —
     done 2026-09-10, see §4. **It mostly does not: 2/10.** The lever is not "index the literals
     pylint's issues already contain" — that premise is mostly false. Either a smaller, more
     targeted use of literal seeding (only the 2/10 it demonstrably helps) or a different lever
     entirely is needed; this doesn't reopen the seeding-vs-traversal question, it narrows it.
   - ~~File-diversity in snippet selection~~ — done 2026-09-11, see §4. Real, but **does not
     bear on k≤3**: it's a gap between `.rank()`'s unbounded ranking and what `.retrieve()`
     actually delivers at its own k (default 12), not between graph and seeds at small k. Fixed
     regardless, since it's what an eventual Phase-3 agent would actually receive.
2. ~~Settle the `min_confidence` finding~~ — done, it did not replicate; default stays. Don't
   re-open without a much larger n.
3. ~~Architecture diagram for the README~~ — done, and generated by `cartographer graph` from
   this repo's own import graph rather than drawn by hand.
4. ~~Phase 2~~ — done 2026-09-09. `cartographer score --instance-id <id> --gold --run-id <id>`
   runs the acceptance check by hand against real Docker.
5. ~~Phase 4 retrieval-side comparison~~ — done 2026-09-10, see §4. The graph-vs-embedding number
   is real but mixed (graph wins clearly at k≤3, embedding edges ahead at k=10 inside noise). Does
   **not** close out Phase 4's stated goal ("the comparison") — that's resolved-rate, which needs
   Phase 3.
6. **Phase 3** once the spending decision is made (§7) — this is now the only thing standing
   between the current state and a real end-to-end resolved-rate number, and the only remaining
   blocker on the project's actual thesis (§1). Once it lands, `eval/run_eval.py` (the ≥50-task
   sweep, not created yet) is the last piece: run both retrievers through the same agent loop and
   score both through the real sandbox.
