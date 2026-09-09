"""Subprocess entry point that runs the real SWE-bench harness with Windows'
text-mode line-ending translation turned off.

## The defect this works around

`swebench.harness.run_evaluation` writes `eval.sh` (and the prediction's
`patch.diff`) with `Path.write_text(data)` -- no `newline=""`. On Windows that
call opens the file in text mode with `newline=None`, which silently rewrites
every `\\n` the harness wrote as `\\r\\n` on disk. The harness then copies that
file byte-for-byte into the Linux container and runs `/bin/bash /eval.sh`.
Bash does not strip the `\\r`, so it becomes part of every path and command:
`cd /testbed\\r` doesn't exist, `pytest\\r` isn't found, `git status\\r` isn't a
git command. Confirmed 2026-09-09 against the real harness: a genuine SWE-bench
gold patch -- which by definition makes the target test suite pass -- scored
**unresolved**, because the test suite never actually ran. The container log
showed `CondaError`, `pathspec ... did not match any file`, and
`pytest: command not found` in sequence, which is CRLF corruption cascading
through every line, not a real grading failure.

This is a defect in running an unmodified `swebench` package under a native
Windows Python -- not in `swebench`'s own logic and not something to patch by
hand-editing an installed package (invisible to `uv sync`, silently undone by
the next reinstall). The fix belongs at the boundary where *we* invoke it.

## The fix

Monkeypatch `pathlib.Path.write_text` to default `newline=""` -- no
translation -- before importing the harness, in a subprocess dedicated to this
one call so the patch cannot leak into anything else this process does.
`newline=""` is the same contract `.gitattributes` enforces for tracked files
(`* text eol=lf`); this closes the identical gap for files the harness writes
itself at run time, which `.gitattributes` cannot reach.

An explicit `newline=` argument from a caller is still honoured -- this only
changes the *default*, so a caller that already asked for CRLF keeps getting it.
"""

from __future__ import annotations

import argparse
import pathlib

_orig_write_text = pathlib.Path.write_text


def _write_text_no_translation(
    self: pathlib.Path,
    data: str,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
) -> int:
    if newline is None:
        newline = ""
    return _orig_write_text(self, data, encoding=encoding, errors=errors, newline=newline)


def _install_patch() -> None:
    pathlib.Path.write_text = _write_text_no_translation  # type: ignore[method-assign]


def _main() -> None:
    _install_patch()
    # Imported after the patch so every `write_text` call the harness makes --
    # including ones in modules imported transitively -- goes through the
    # patched method rather than the one bound at their own import time.
    from swebench.harness.run_evaluation import main

    p = argparse.ArgumentParser()
    p.add_argument("--dataset_name", required=True)
    p.add_argument("--split", required=True)
    p.add_argument("--predictions_path", required=True)
    p.add_argument("--run_id", required=True)
    p.add_argument("--report_dir", required=True)
    p.add_argument("--max_workers", type=int, required=True)
    p.add_argument("--timeout", type=int, required=True)
    p.add_argument("--instance_ids", nargs="*", default=None)
    args = p.parse_args()

    main(
        dataset_name=args.dataset_name,
        split=args.split,
        instance_ids=args.instance_ids,
        predictions_path=args.predictions_path,
        max_workers=args.max_workers,
        open_file_limit=4096,
        run_id=args.run_id,
        timeout=args.timeout,
        rewrite_reports=False,
        modal=False,
        report_dir=args.report_dir,
        task_repo=None,
    )


if __name__ == "__main__":
    _main()
