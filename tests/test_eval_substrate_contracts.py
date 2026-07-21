from __future__ import annotations

import pytest

from eval_suite.schemas.eval_substrate_contracts import (
    FAILURE_CLASSES,
    REQUIRED_RESULT_ROW_FIELDS,
    REQUIRED_TASK_PACK_FIELDS,
    result_row_verdict,
    validate_result_row,
    validate_task_pack,
)
from harness.aether2.runtime.route_schemas import SchemaValidationError


def _task_pack() -> dict:
    return {
        "task_id": "synthetic-substrate-smoke",
        "task_prompt": "Write the expected token into the workspace.",
        "fixture": {"type": "synthetic", "workspace_ref": "fixtures/synthetic"},
        "canonical_root": "/app",
        "backend_requirements": {"backend_type": "docker", "network": "disabled"},
        "visible_verifier": {"command": "python3 verifier.py"},
        "hidden_verifier": {
            "command_shape": "python3 hidden_verifier.py --case <case_ref>",
            "checks_ref": "hidden://synthetic-substrate-smoke",
            "leak_hidden_checks_to_prompt": False,
        },
        "grader": {"type": "deterministic", "score_range": [0, 1]},
        "contamination_policy": {"status": "clean", "source": "original_synthetic"},
        "artifact_capture_policy": {"capture": ["environment", "artifacts", "trace"]},
        "admission_level": "draft",
        "surface_type": "synthetic_substrate_smoke",
    }


def _result_row() -> dict:
    return {
        "run_id": "run-pass",
        "eval_id": "eval-substrate-smoke",
        "task_pack_id": "synthetic-substrate-smoke",
        "family": "synthetic_substrate",
        "surface_type": "synthetic_substrate_smoke",
        "admission_level": "draft",
        "backend_ref": "debug_local_no_sandbox",
        "environment_ref": "artifacts/environment_manifest.json",
        "artifact_refs": ["artifacts/artifact_bundle.json"],
        "trace_refs": ["traces/trace.json"],
        "closure_status": "closed",
        "task_truth_status": "pass",
        "contamination_status": "clean",
        "failure_class": "none",
        "reason_codes": [],
        "verifier_ref": "artifacts/verifier_output.json",
        "grader_ref": "artifacts/grader_output.json",
        "score": 1.0,
    }


def test_task_pack_contract_accepts_required_fields():
    validated = validate_task_pack(_task_pack())

    assert tuple(REQUIRED_TASK_PACK_FIELDS)
    assert validated["canonical_root"] == "/app"
    assert validated["hidden_verifier"]["leak_hidden_checks_to_prompt"] is False


def test_task_pack_rejects_hidden_check_leakage():
    task_pack = _task_pack()
    task_pack["hidden_verifier"]["leak_hidden_checks_to_prompt"] = True

    with pytest.raises(SchemaValidationError, match="leak_hidden_checks_to_prompt"):
        validate_task_pack(task_pack)


def test_result_row_contract_accepts_required_fields_and_labels():
    validated = validate_result_row(_result_row())

    assert tuple(REQUIRED_RESULT_ROW_FIELDS)
    assert set(FAILURE_CLASSES) >= {"schema_parsing", "evidence_acquisition", "unclear"}
    assert validated["score"] == 1.0
    assert result_row_verdict(validated) == "pass"


def test_result_row_known_bad_failure_verdict():
    row = _result_row()
    row.update(
        {
            "run_id": "run-known-bad",
            "task_truth_status": "fail",
            "failure_class": "verification_grading",
            "reason_codes": ["known_bad_visible_verifier_failed"],
            "score": 0.0,
        }
    )

    assert validate_result_row(row)["failure_class"] == "verification_grading"
    assert result_row_verdict(row) == "fail"


def test_result_row_rejects_missing_required_field():
    row = _result_row()
    row.pop("trace_refs")

    with pytest.raises(SchemaValidationError, match="trace_refs"):
        validate_result_row(row)
