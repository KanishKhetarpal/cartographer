"""Score a patch against SWE-bench's own Docker harness.

Deliberately thin. The kickoff's guardrail is explicit: don't hand-roll verdict
logic. `swebench.harness.run_evaluation.main` already builds the container,
applies the patch, runs FAIL_TO_PASS/PASS_TO_PASS, and classifies the result --
this module's only job is to hand it a predictions file in the shape it wants
and read back the report it writes, so a wrong verdict is a bug in code we did
not write rather than one we did.

## What "image" means here (as of swebench 5.0.2)

The harness no longer builds an instance's environment from a Dockerfile spec.
Every row of `SWE-bench/SWE-bench_Verified` on Hugging Face now carries a
pre-built `image` field (`swebench/sweb.eval.x86_64.<id>:latest`) that Docker
pulls from Docker Hub. That is a real change from the kickoff's mental model
of "install the repo at the base commit" -- there is no repo checkout on our
side at all for scoring; the checkout already happened when SWE-bench built
the image. Confirmed 2026-09-09: our local `_fixtures/swebench_verified.json`
predates this and lacks `image` entirely (`KeyError: 'image'`), so scoring
must go through the HF dataset name, not that fixture. The fixture stays
correct for Phase 1 (it never needed `image`); this module never reads it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: The dataset every score is measured against. Passed straight to `datasets`,
#: which caches the parquet locally after the first pull -- no re-download per
#: run. Overridable for a smoke test against a narrower split, never for a
#: number that goes in REPORT.md.
DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_SPLIT = "test"

#: A patch dict identifying itself as gold rather than a real attempt would let
#: a placeholder run be graded against the answer key and reported as a score.
_RESERVED_MODEL_NAMES = {"gold", "none"}


@dataclass(frozen=True, slots=True)
class Prediction:
    instance_id: str
    patch: str
    model_name: str

    def __post_init__(self) -> None:
        if self.model_name.lower() in _RESERVED_MODEL_NAMES:
            raise ValueError(
                f"model_name {self.model_name!r} is reserved by the harness "
                "('gold' means 'skip predictions_path and grade the answer key')"
            )


@dataclass(frozen=True, slots=True)
class RunReport:
    """Mirrors `swebench.harness.reporting.make_run_report`'s JSON, read back
    off disk rather than re-derived, so this module can disagree with the
    harness about nothing."""

    total_instances: int
    submitted_instances: int
    resolved_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    error_ids: tuple[str, ...]
    empty_patch_ids: tuple[str, ...]
    raw: dict

    @property
    def resolved_rate(self) -> float:
        """Of what was actually submitted -- an empty-patch or error instance
        should lower this, an instance nobody attempted should not, or a
        partial eval run would read as a worse score than it is."""
        if not self.submitted_instances:
            return 0.0
        return len(self.resolved_ids) / self.submitted_instances

    @classmethod
    def from_json(cls, path: Path) -> RunReport:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            total_instances=data["total_instances"],
            submitted_instances=data["submitted_instances"],
            resolved_ids=tuple(data["resolved_ids"]),
            unresolved_ids=tuple(data["unresolved_ids"]),
            error_ids=tuple(data["error_ids"]),
            empty_patch_ids=tuple(data.get("empty_patch_ids", ())),
            raw=data,
        )


def write_predictions(preds: list[Prediction], path: Path) -> Path:
    """The shape `get_predictions_from_file` reads: instance_id -> {instance_id,
    model_patch, model_name_or_path}, as a JSON list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "instance_id": p.instance_id,
                    "model_patch": p.patch,
                    "model_name_or_path": p.model_name,
                }
                for p in preds
            ]
        ),
        encoding="utf-8",
    )
    return path


def run(
    predictions_path: Path | str,
    *,
    run_id: str,
    instance_ids: list[str] | None = None,
    dataset_name: str = DEFAULT_DATASET,
    split: str = DEFAULT_SPLIT,
    report_dir: Path | str = ".",
    max_workers: int = 4,
    timeout: int = 1800,
) -> RunReport:
    """Invoke the real harness as a subprocess, not an in-process import.

    In-process was tried first and dropped: `run_evaluation.main` calls
    `resource.setrlimit` and drives a Docker client with module-level state
    that is not meant to run twice in one interpreter (an eval sweep calls
    this once per configuration). A subprocess also means a harness crash --
    seen once already, `KeyError: 'image'` against the stale local fixture --
    surfaces as a nonzero exit this function raises on, rather than as a
    half-populated report this function would otherwise have to guess about.

    Goes through `_win_launcher.py`, not `-m swebench.harness.run_evaluation`
    directly. On Windows, the plain module invocation writes `eval.sh` with
    every `\\n` silently turned into `\\r\\n`, which fails a real gold patch
    against a real instance -- confirmed 2026-09-09, see that module's
    docstring. The launcher is a thin argparse shim in front of the identical
    `main()` call, with the write-mode fix installed before `swebench` is
    imported.
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "cartographer.sandbox._win_launcher",
        "--dataset_name", dataset_name,
        "--split", split,
        "--predictions_path", str(predictions_path),
        "--run_id", run_id,
        "--report_dir", str(report_dir),
        "--max_workers", str(max_workers),
        "--timeout", str(timeout),
    ]
    if instance_ids:
        cmd += ["--instance_ids", *instance_ids]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"swebench harness exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}"
        )

    report_path = report_dir / f"{run_id}.json"
    if not report_path.exists():
        # The harness names the report after model_name_or_path when there is
        # exactly one, per `reporting.py`; fall back to a glob rather than
        # guessing the exact name and silently reading nothing.
        candidates = sorted(report_dir.glob(f"*.{run_id}.json")) or sorted(
            report_dir.glob(f"{run_id}*.json")
        )
        if not candidates:
            raise FileNotFoundError(
                f"harness exited 0 but wrote no report under {report_dir} for run_id={run_id!r}"
            )
        report_path = candidates[0]
    return RunReport.from_json(report_path)
