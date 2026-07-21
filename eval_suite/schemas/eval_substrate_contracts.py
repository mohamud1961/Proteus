"""Eval substrate contracts for task packs and result rows."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from harness.aether2.runtime.route_schemas import SchemaValidationError

REQUIRED_TASK_PACK_FIELDS = (
    "task_id",
    "task_prompt",
    "fixture",
    "canonical_root",
    "backend_requirements",
    "visible_verifier",
    "hidden_verifier",
    "grader",
    "contamination_policy",
    "artifact_capture_policy",
    "admission_level",
    "surface_type",
)

REQUIRED_RESULT_ROW_FIELDS = (
    "run_id",
    "eval_id",
    "task_pack_id",
    "family",
    "surface_type",
    "admission_level",
    "backend_ref",
    "environment_ref",
    "artifact_refs",
    "trace_refs",
    "closure_status",
    "task_truth_status",
    "contamination_status",
    "failure_class",
    "reason_codes",
    "verifier_ref",
    "grader_ref",
    "score",
)

ADMISSION_LEVELS = ("draft", "diagnostic", "certified")
SURFACE_TYPES = (
    "terminal",
    "filesystem",
    "tool_call",
    "retrieval",
    "verifier_repair",
    "synthetic_substrate_smoke",
)
CLOSURE_STATUSES = ("closed", "open", "invalid")
TASK_TRUTH_STATUSES = ("pass", "fail", "invalid")
CONTAMINATION_STATUSES = ("clean", "suspect", "contaminated", "unknown")
FAILURE_CLASSES = (
    "none",
    "path_cwd",
    "runtime",
    "provider",
    "tool_contract",
    "sandbox",
    "verification_grading",
    "schema_parsing",
    "agent_initialization",
    "evidence_acquisition",
    "reduction_selection",
    "model_capability",
    "unclear",
)


def validate_task_pack(task_pack: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(task_pack, "task_pack")
    _require_fields(data, REQUIRED_TASK_PACK_FIELDS, "task_pack")
    for field in ("task_id", "task_prompt", "canonical_root"):
        _require_string(data[field], f"task_pack.{field}")
    if data["canonical_root"] != "/app":
        raise SchemaValidationError("task_pack.canonical_root must normalize to /app")
    _require_mapping(data["fixture"], "task_pack.fixture")
    _require_mapping(data["backend_requirements"], "task_pack.backend_requirements")
    _require_mapping(data["visible_verifier"], "task_pack.visible_verifier")
    _require_mapping(data["hidden_verifier"], "task_pack.hidden_verifier")
    _require_mapping(data["grader"], "task_pack.grader")
    _require_mapping(data["contamination_policy"], "task_pack.contamination_policy")
    _require_mapping(data["artifact_capture_policy"], "task_pack.artifact_capture_policy")
    _require_enum(data["admission_level"], "task_pack.admission_level", ADMISSION_LEVELS)
    _require_enum(data["surface_type"], "task_pack.surface_type", SURFACE_TYPES)
    _require_string(data["visible_verifier"].get("command"), "task_pack.visible_verifier.command")
    _require_string(data["hidden_verifier"].get("command_shape"), "task_pack.hidden_verifier.command_shape")
    if data["hidden_verifier"].get("leak_hidden_checks_to_prompt") is not False:
        raise SchemaValidationError(
            "task_pack.hidden_verifier.leak_hidden_checks_to_prompt must be false"
        )
    return deepcopy(data)


def validate_result_row(row: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(row, "result_row")
    _require_fields(data, REQUIRED_RESULT_ROW_FIELDS, "result_row")
    for field in (
        "run_id",
        "eval_id",
        "task_pack_id",
        "family",
        "backend_ref",
        "environment_ref",
        "verifier_ref",
        "grader_ref",
    ):
        _require_string(data[field], f"result_row.{field}")
    _require_enum(data["surface_type"], "result_row.surface_type", SURFACE_TYPES)
    _require_enum(data["admission_level"], "result_row.admission_level", ADMISSION_LEVELS)
    _require_enum(data["closure_status"], "result_row.closure_status", CLOSURE_STATUSES)
    _require_enum(data["task_truth_status"], "result_row.task_truth_status", TASK_TRUTH_STATUSES)
    _require_enum(
        data["contamination_status"],
        "result_row.contamination_status",
        CONTAMINATION_STATUSES,
    )
    _require_enum(data["failure_class"], "result_row.failure_class", FAILURE_CLASSES)
    _require_string_list(data["artifact_refs"], "result_row.artifact_refs")
    _require_string_list(data["trace_refs"], "result_row.trace_refs")
    _require_string_list(data["reason_codes"], "result_row.reason_codes", allow_empty=True)
    score = data["score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise SchemaValidationError("result_row.score must be a number")
    if score < 0 or score > 1:
        raise SchemaValidationError("result_row.score must be between 0 and 1")
    return deepcopy(data)


def result_row_verdict(row: dict[str, Any]) -> str:
    data = validate_result_row(row)
    if data["closure_status"] == "invalid" or data["task_truth_status"] == "invalid":
        return "invalid"
    if data["task_truth_status"] == "pass" and data["score"] == 1:
        return "pass"
    return "fail"


def _require_fields(data: dict[str, Any], fields: tuple[str, ...], path: str) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise SchemaValidationError(f"{path} missing required fields: {', '.join(missing)}")


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{path} must be an object")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaValidationError(f"{path} must be a non-empty string")
    return value


def _require_string_list(value: Any, path: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"{path} must be a list")
    if not value and not allow_empty:
        raise SchemaValidationError(f"{path} must not be empty")
    for index, item in enumerate(value):
        _require_string(item, f"{path}[{index}]")
    return value


def _require_enum(value: Any, path: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise SchemaValidationError(f"{path} must be one of {allowed}")
    return value
