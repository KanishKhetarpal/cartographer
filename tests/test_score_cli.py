"""`cartographer score` -- the argument handling only. The real harness call
is mocked here (it costs Docker and minutes); the harness itself is verified
separately against real containers, see `cartographer/sandbox/_win_launcher.py`
and CONTEXT.md's Phase 2 section for that evidence.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cartographer.cli import app
from cartographer.sandbox.docker_runner import RunReport

runner = CliRunner()


def _fake_report(*, resolved: bool) -> RunReport:
    iid = "django__django-16082"
    return RunReport(
        total_instances=1,
        submitted_instances=1,
        resolved_ids=(iid,) if resolved else (),
        unresolved_ids=() if resolved else (iid,),
        error_ids=(),
        empty_patch_ids=(),
        raw={},
    )


def test_score_requires_exactly_one_of_patch_or_gold(tmp_path):
    res = runner.invoke(app, ["score", "--instance-id", "x", "--run-id", "r"])
    assert res.exit_code == 2


def test_score_rejects_both_patch_and_gold(tmp_path):
    patch_file = tmp_path / "p.diff"
    patch_file.write_text("diff", encoding="utf-8")
    res = runner.invoke(
        app,
        ["score", "--instance-id", "x", "--run-id", "r", "--patch", str(patch_file), "--gold"],
    )
    assert res.exit_code == 2


def test_score_gold_reports_resolved(tmp_path):
    with patch("cartographer.sandbox.docker_runner.run", return_value=_fake_report(resolved=True)):
        res = runner.invoke(
            app,
            ["score", "--instance-id", "django__django-16082", "--run-id", "r", "--gold",
             "--report-dir", str(tmp_path)],
        )
    assert res.exit_code == 0, res.output
    assert "RESOLVED" in res.output


def test_score_reports_unresolved_without_raising(tmp_path):
    with patch("cartographer.sandbox.docker_runner.run", return_value=_fake_report(resolved=False)):
        res = runner.invoke(
            app,
            ["score", "--instance-id", "django__django-16082", "--run-id", "r", "--gold",
             "--report-dir", str(tmp_path)],
        )
    assert res.exit_code == 0, res.output
    assert "unresolved" in res.output


def test_score_with_a_patch_file_writes_predictions_and_calls_the_harness(tmp_path):
    patch_file = tmp_path / "p.diff"
    patch_file.write_text("diff --git a/x b/x\n", encoding="utf-8")

    mock_run = MagicMock(return_value=_fake_report(resolved=True))
    with patch("cartographer.sandbox.docker_runner.run", mock_run):
        res = runner.invoke(
            app,
            ["score", "--instance-id", "django__django-16082", "--run-id", "r",
             "--patch", str(patch_file), "--report-dir", str(tmp_path)],
        )
    assert res.exit_code == 0, res.output
    mock_run.assert_called_once()
    predictions_path = mock_run.call_args.args[0]
    import json

    data = json.loads(predictions_path.read_text(encoding="utf-8"))
    assert data[0]["model_patch"] == "diff --git a/x b/x\n"
    assert data[0]["instance_id"] == "django__django-16082"
