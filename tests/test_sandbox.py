"""Unit tests for the harness wrapper.

Deliberately no test here calls the real Docker harness -- that costs minutes
and a multi-GB image pull, and belongs in a manual/CI-gated smoke check, not
the fast suite. These pin the two things this module owns: building the
predictions file the harness expects, and refusing a model name the harness
would silently misinterpret as "skip grading and use the answer key."
"""

from __future__ import annotations

import json

import pytest

from cartographer.sandbox.docker_runner import Prediction, RunReport, write_predictions


@pytest.mark.parametrize("reserved", ["gold", "Gold", "GOLD", "none", "None"])
def test_a_prediction_cannot_claim_a_reserved_model_name(reserved):
    """'gold' tells the harness to ignore predictions_path and grade the
    answer key -- a real attempt claiming that name would be graded as a
    correct patch it never produced."""
    with pytest.raises(ValueError, match="reserved"):
        Prediction(instance_id="x", patch="diff", model_name=reserved)


def test_an_ordinary_model_name_is_accepted():
    Prediction(instance_id="x", patch="diff", model_name="cartographer-graph")


def test_write_predictions_matches_the_harness_shape(tmp_path):
    preds = [
        Prediction(instance_id="repo__1", patch="diff --git a b", model_name="m"),
        Prediction(instance_id="repo__2", patch="", model_name="m"),
    ]
    out = write_predictions(preds, tmp_path / "preds.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == [
        {"instance_id": "repo__1", "model_patch": "diff --git a b", "model_name_or_path": "m"},
        {"instance_id": "repo__2", "model_patch": "", "model_name_or_path": "m"},
    ]


def test_write_predictions_creates_parent_dirs(tmp_path):
    out = write_predictions(
        [Prediction(instance_id="x", patch="d", model_name="m")],
        tmp_path / "nested" / "dir" / "preds.json",
    )
    assert out.exists()


def test_run_report_resolved_rate_is_over_submitted_not_total():
    """A partial eval run (2 of 500 submitted) must not read as a 0.4% score --
    the denominator is what was actually attempted."""
    report = RunReport(
        total_instances=500,
        submitted_instances=2,
        resolved_ids=("a",),
        unresolved_ids=("b",),
        error_ids=(),
        empty_patch_ids=(),
        raw={},
    )
    assert report.resolved_rate == 0.5


def test_run_report_resolved_rate_is_zero_when_nothing_submitted():
    report = RunReport(
        total_instances=500,
        submitted_instances=0,
        resolved_ids=(),
        unresolved_ids=(),
        error_ids=(),
        empty_patch_ids=(),
        raw={},
    )
    assert report.resolved_rate == 0.0


def test_run_report_from_json_reads_the_harness_shape(tmp_path):
    """Pinned against `make_run_report`'s actual field names (schema_version 2)
    -- a key renamed upstream should fail this test, not silently read as 0."""
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "total_instances": 500,
                "submitted_instances": 1,
                "completed_instances": 1,
                "resolved_instances": 1,
                "unresolved_instances": 0,
                "resolved_ids": ["scikit-learn__scikit-learn-14141"],
                "unresolved_ids": [],
                "error_ids": [],
                "empty_patch_ids": [],
                "schema_version": 2,
            }
        ),
        encoding="utf-8",
    )
    report = RunReport.from_json(path)
    assert report.resolved_ids == ("scikit-learn__scikit-learn-14141",)
    assert report.resolved_rate == 1.0
    assert report.raw["schema_version"] == 2
