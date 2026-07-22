from __future__ import annotations

import pytest

from eval_suite.adapters.contracts import (
    aggregate_adapter_result_rows,
    build_adapter_result_row,
    contamination_status_from_labels,
    normalize_model_output,
    validate_adapter_result_row,
    validate_benchmark_adapter_case,
)
from eval_suite.schemas.eval_substrate_contracts import validate_result_row
from harness.aether2.runtime.route_schemas import SchemaValidationError


def _task_pack() -> dict:
    return {
        "task_id": "bfcl_case_001",
        "task_prompt": "Emit ordered tool calls for the provided conversation.",
        "fixture": {"type": "json_fixture", "workspace_ref": "fixtures/bfcl/case_001"},
        "canonical_root": "/app",
        "backend_requirements": {"backend_type": "docker", "network": "disabled"},
        "visible_verifier": {"command": "python3 verifier.py"},
        "hidden_verifier": {
            "command_shape": "python3 hidden_verifier.py --case <case_ref>",
            "checks_ref": "hidden://bfcl/case_001",
            "leak_hidden_checks_to_prompt": False,
        },
        "grader": {"type": "deterministic", "score_range": [0, 1]},
        "contamination_policy": {"status": "clean", "source": "curated"},
        "artifact_capture_policy": {"capture": ["environment", "artifacts", "trace"]},
        "admission_level": "certified",
        "surface_type": "tool_call",
    }


def _benchmark_case() -> dict:
    return {
        "benchmark_family": "bfcl",
        "benchmark_case_id": "multi_turn_composite_97",
        "authority_label": "equivalent",
        "surface_type": "tool_call",
        "admission_level": "certified",
        "expected_answer": {"format": "tool_call_sequence", "value": "ground_truth_calls"},
        "contamination_labels": ["clean", "curated_external_subset"],
        "certified_task_pack": _task_pack(),
    }


def test_validate_benchmark_adapter_case_accepts_bfcl_equivalent_task_pack():
    validated = validate_benchmark_adapter_case(_benchmark_case())

    assert validated["authority_label"] == "equivalent"
    assert validated["benchmark_family"] == "bfcl"


def test_validate_benchmark_adapter_case_accepts_execution_unit_path():
    case = _benchmark_case()
    case.pop("certified_task_pack")
    case["execution_unit"] = {
        "unit_id": "contextbench_verified_03",
        "task_prompt": "Read /contextbench/Verified.csv and return the required structured row.",
        "canonical_root": "/app",
        "execution_contract": {"runner": "contextbench_row_reader", "timeout_sec": 60},
    }

    validated = validate_benchmark_adapter_case(case)

    assert validated["execution_unit"]["unit_id"] == "contextbench_verified_03"


def test_validate_benchmark_adapter_case_requires_mapping_target():
    case = _benchmark_case()
    case.pop("certified_task_pack")
    with pytest.raises(SchemaValidationError, match="certified_task_pack or execution_unit"):
        validate_benchmark_adapter_case(case)


def test_validate_benchmark_adapter_case_rejects_unsupported_surface_type():
    case = _benchmark_case()
    case["surface_type"] = "chat_only"
    with pytest.raises(SchemaValidationError, match="benchmark_case.surface_type"):
        validate_benchmark_adapter_case(case)


def test_validate_benchmark_adapter_case_rejects_unsupported_admission_level():
    case = _benchmark_case()
    case["admission_level"] = "production"
    with pytest.raises(SchemaValidationError, match="benchmark_case.admission_level"):
        validate_benchmark_adapter_case(case)


def test_contamination_status_from_labels_contract():
    assert contamination_status_from_labels(["clean"]) == "clean"
    assert contamination_status_from_labels(["possible_overlap", "custom_homolog"]) == "suspect"
    assert contamination_status_from_labels(["benchmark_copy", "ground_truth_exposed"]) == "contaminated"
    assert contamination_status_from_labels(["custom_unlabeled"]) == "unknown"


def test_normalize_model_output_supports_json_and_tool_call_sequence():
    json_output = normalize_model_output(
        {"assistant_text": '{"a": 1, "b": 2}'},
        {"format": "json", "value": {"schema": "object"}},
    )
    tool_output = normalize_model_output(
        {"tool_calls": [{"name": "find_user", "arguments": {"user_id": "42"}}]},
        {"format": "tool_call_sequence", "value": "ordered"},
    )

    assert json_output["value"] == {"a": 1, "b": 2}
    assert "find_user" in tool_output["value"][0]


def test_build_adapter_result_row_stays_substrate_compatible():
    row = build_adapter_result_row(
        run_id="run-bfcl-pass",
        eval_id="bfcl_v3_strict_multi_turn_composite_97",
        task_pack_id="bfcl_case_001",
        backend_ref="backend/docker",
        environment_ref="artifacts/environment_manifest.json",
        verifier_ref="artifacts/verifier_output.json",
        grader_ref="artifacts/grader_output.json",
        benchmark_case=_benchmark_case(),
        native_grader_output={"verdict": "pass", "reason_codes": [], "score": 1.0},
        trace_refs=["traces/trace.json"],
        artifact_refs=["artifacts/artifact_bundle.json"],
    )

    validated = validate_adapter_result_row(row)
    assert validated["task_truth_status"] == "pass"
    assert validated["authority_label"] == "equivalent"
    assert validated["contamination_status"] == "clean"
    assert validated["native_certification_status"] == "equivalent_or_shaped"
    assert validated["native_promotion_eligible"] is False


def test_build_adapter_result_row_marks_native_debug_rows_nonpromotable():
    native_case = dict(_benchmark_case(), authority_label="native", benchmark_case_id="native_debug_case")
    native_case["admission_level"] = "diagnostic"
    row = build_adapter_result_row(
        run_id="run-native-debug-pass",
        eval_id="native_debug_eval",
        task_pack_id="native_debug_task",
        backend_ref="debug_local_no_sandbox",
        environment_ref="artifacts/environment_manifest.json",
        verifier_ref="artifacts/verifier_output.json",
        grader_ref="artifacts/grader_output.json",
        benchmark_case=native_case,
        native_grader_output={"verdict": "pass", "reason_codes": [], "score": 1.0},
        trace_refs=["traces/trace.json"],
        artifact_refs=["artifacts/artifact_bundle.json"],
    )

    validated = validate_adapter_result_row(row)
    assert validated["native_certification_status"] == "native_noncertified_context"
    assert validated["native_promotion_eligible"] is False


def test_build_adapter_result_row_marks_certified_native_pass_promotable():
    native_case = dict(_benchmark_case(), authority_label="native", benchmark_case_id="native_certified_case")
    row = build_adapter_result_row(
        run_id="run-native-certified-pass",
        eval_id="native_certified_eval",
        task_pack_id="native_certified_task",
        backend_ref="azure_vm_docker",
        environment_ref="artifacts/environment_manifest.json",
        verifier_ref="artifacts/verifier_output.json",
        grader_ref="artifacts/grader_output.json",
        benchmark_case=native_case,
        native_grader_output={"verdict": "pass", "reason_codes": [], "score": 1.0},
        trace_refs=["traces/trace.json"],
        artifact_refs=["artifacts/artifact_bundle.json"],
    )

    validated = validate_adapter_result_row(row)
    assert validated["native_certification_status"] == "native_certified_pass"
    assert validated["native_promotion_eligible"] is True


def test_aggregate_adapter_result_rows_groups_by_authority():
    base_case = _benchmark_case()
    native_case = dict(base_case, authority_label="native", benchmark_case_id="native_case")
    shaped_case = dict(base_case, authority_label="shaped", benchmark_case_id="shaped_case")

    rows = [
        build_adapter_result_row(
            run_id="run-native-pass",
            eval_id="native_eval",
            task_pack_id="native_task",
            backend_ref="backend/docker",
            environment_ref="artifacts/env_native.json",
            verifier_ref="artifacts/verifier_native.json",
            grader_ref="artifacts/grader_native.json",
            benchmark_case=native_case,
            native_grader_output={"verdict": "pass", "reason_codes": [], "score": 1.0},
            trace_refs=["traces/native_trace.json"],
            artifact_refs=["artifacts/native_bundle.json"],
        ),
        build_adapter_result_row(
            run_id="run-equiv-fail",
            eval_id="equiv_eval",
            task_pack_id="equiv_task",
            backend_ref="backend/docker",
            environment_ref="artifacts/env_equiv.json",
            verifier_ref="artifacts/verifier_equiv.json",
            grader_ref="artifacts/grader_equiv.json",
            benchmark_case=base_case,
            native_grader_output={
                "verdict": "fail",
                "reason_codes": ["bfcl_missing_required_calls"],
                "score": 0.0,
            },
            trace_refs=["traces/equiv_trace.json"],
            artifact_refs=["artifacts/equiv_bundle.json"],
        ),
        build_adapter_result_row(
            run_id="run-shaped-invalid",
            eval_id="shaped_eval",
            task_pack_id="shaped_task",
            backend_ref="backend/docker",
            environment_ref="artifacts/env_shaped.json",
            verifier_ref="artifacts/verifier_shaped.json",
            grader_ref="artifacts/grader_shaped.json",
            benchmark_case=shaped_case,
            native_grader_output={"verdict": "invalid", "reason_codes": ["grader_timeout"], "score": 0.0},
            trace_refs=["traces/shaped_trace.json"],
            artifact_refs=["artifacts/shaped_bundle.json"],
            failure_class="runtime",
        ),
    ]

    scoreboard = aggregate_adapter_result_rows(rows)

    assert scoreboard["row_count"] == 3
    assert scoreboard["totals"] == {"pass": 1, "fail": 1, "invalid": 1, "total": 3}
    assert scoreboard["by_authority_label"]["native"]["pass"] == 1
    assert scoreboard["by_authority_label"]["equivalent"]["fail"] == 1
    assert scoreboard["by_authority_label"]["shaped"]["invalid"] == 1


def test_result_row_can_represent_agent_initialization_failure_without_task_fail():
    row = {
        "run_id": "run-init-failure",
        "eval_id": "aether2_init_failure_contract",
        "task_pack_id": "task-pack",
        "family": "initialization",
        "surface_type": "terminal",
        "admission_level": "diagnostic",
        "backend_ref": "debug_local_no_sandbox",
        "environment_ref": "artifacts/environment_manifest.json",
        "artifact_refs": ["artifacts/agent_initialization_failure.json"],
        "trace_refs": ["traces/init_failure.json"],
        "closure_status": "invalid",
        "task_truth_status": "invalid",
        "contamination_status": "clean",
        "failure_class": "agent_initialization",
        "reason_codes": ["architect_config_schema_invalid_after_retry"],
        "verifier_ref": "artifacts/verifier_not_run.json",
        "grader_ref": "artifacts/grader_not_run.json",
        "score": 0.0,
    }

    validated = validate_result_row(row)

    assert validated["closure_status"] == "invalid"
    assert validated["task_truth_status"] == "invalid"
    assert validated["failure_class"] == "agent_initialization"
