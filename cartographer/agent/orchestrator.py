"""The agent loop: plan -> retrieve -> edit -> run_tests -> reflect.

PHASE 0 STUB. Straight-line and synchronous: it retrieves, then emits a
placeholder patch. LangGraph, the iteration budget and the sandbox arrive in
Phase 3 -- this exists now to pin the *shape* of the loop's inputs and outputs
so the CLI and the eval harness can be built against a stable contract.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..retrieval.base import Context, Issue, RepoRef, Retriever

STUB_PATCH = """\
diff --git a/PLACEHOLDER b/PLACEHOLDER
--- a/PLACEHOLDER
+++ b/PLACEHOLDER
@@ -0,0 +1 @@
+# phase-0 stub: no model was called, no edit was made
"""


@dataclass(slots=True)
class Step:
    node: str
    detail: str
    elapsed_s: float


@dataclass(slots=True)
class Result:
    issue_id: str
    mode: str
    patch: str | None
    iterations: int
    context: Context
    trace: list[Step] = field(default_factory=list)
    stub: bool = False
    stats: dict[str, Any] = field(default_factory=dict)


def resolve(
    issue: Issue,
    repo: RepoRef,
    retriever: Retriever,
    *,
    max_iterations: int = 4,
    budget_tokens: int = 8000,
) -> Result:
    trace: list[Step] = []

    t0 = time.perf_counter()
    ctx = retriever.retrieve(issue, repo, budget_tokens=budget_tokens)
    trace.append(
        Step("retrieve", f"{len(ctx.snippets)} snippets across {len(ctx.files)} files",
             time.perf_counter() - t0)
    )

    t1 = time.perf_counter()
    trace.append(Step("edit", "stub patch emitted; no model call", time.perf_counter() - t1))

    return Result(
        issue_id=issue.id,
        mode=ctx.mode,
        patch=STUB_PATCH,
        iterations=1,
        context=ctx,
        trace=trace,
        # Phase 0: the loop itself is the stub, so this is unconditional. When
        # the real loop lands this becomes `bool(ctx.stats.get("stub"))` -- the
        # eval harness reads this one flag rather than re-deriving the
        # condition from each component.
        stub=True,
        stats={"max_iterations": max_iterations, "budget_tokens": budget_tokens},
    )
