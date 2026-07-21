"""Contracts for adapting benchmark evals into substrate-native result rows."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable

from eval_suite.schemas.eval_substrate_contracts import (
    ADMISSION_LEVELS,
    SURFACE_TYPES,
    result_row_verdict,
    validate_result_row,
    validate_task_pack,
)
from harness.aether2.runtime.route_schemas import SchemaValidationError

ADAPTER_AUTHORITIES = ("native", "equivalent", "shaped")
EXPECTED_ANSWER_FORMATS = ("text", "json", "tool_call_sequence", "artifact_ref")
EXECUTION_UNIT_REQUIRED_FIELDS = ("unit_id", "task_prompt", "canonical_root", "execution_contract")
BENCHMARK_CASE_REQUIRED_FIELDS = (
    "benchmark_family",
    "benchmark_case_id",
    "authority_label",
    "surface_type",
    "admission_level",
    "expected_answer",
    "contamination_labels",
)
_CONTAMINATED_HINTS = {"contaminated", "leak", "benchmark_copy", "ground_truth_exposed"}
_SUSPECT_HINTS = {"suspect", "reuse_risk", "possible_overlap", "unverified_origin"}


def validate_benchmark_adapter_case(case: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(case, "benchmark_case")
    _require_fields(data, BENCHMARK_CASE_REQUIRED_FIELDS, "benchmark_case")
    for field in ("benchmark_family", "benchmark_case_id"):
        _require_string(data[field], f"benchmark_case.{field}")
    _require_enum(data["authority_label"], "benchmark_case.authority_label", ADAPTER_AUTHORITIES)
    _require_enum(data["surface_type"], "benchmark_case.surface_type", SURFACE_TYPES)
    _require_enum(data["admission_level"], "benchmark_case.admission_level", ADMISSION_LEVELS)
    _require_string_list(data["contamination_labels"], "benchmark_case.contamination_labels")
    _validate_expected_answer(_require_mapping(data["expected_answer"], "benchmark_case.expected_answer"))
    has_task_pack = "certified_task_pack" in data
    has_execution_unit = "execution_unit" in data
    if not has_task_pack and not has_execution_unit:
        raise SchemaValidationError(
            "benchmark_case must include certified_task_pack or execution_unit"
        )
    if has_task_pack:
        validate_task_pack(_require_mapping(data["certified_task_pack"], "benchmark_case.certified_task_pack"))
    if has_execution_unit:
        _validate_execution_unit(_require_mapping(data["execution_unit"], "benchmark_case.execution_unit"))
    return deepcopy(data)


def contamination_status_from_labels(labels: list[str]) -> str:
    lowered = {label.strip().lower() for label in labels if isinstance(label, str)}
    if lowered & _CONTAMINATED_HINTS:
        return "contaminated"
    if lowered & _SUSPECT_HINTS:
        return "suspect"
    if "clean" in lowered:
        return "clean"
    return "unknown"


def normalize_model_output(
    model_output: dict[str, Any],
    expected_answer: dict[str, Any],
) -> dict[str, Any]:
    expected = _validate_expected_answer(expected_answer)
    fmt = expected["format"]
    text = _first_text(model_output)
    if fmt == "text":
        return {"format": fmt, "value": text}
    if fmt == "json":
        parsed = _json_from_output(model_output, text)
        return {"format": fmt, "value": parsed}
    if fmt == "tool_call_sequence":
        return {"format": fmt, "value": _tool_call_sequence(model_output)}
    return {"format": fmt, "value": _artifact_ref(model_output)}


def build_adapter_result_row(
    *,
    run_id: str,
    eval_id: str,
    task_pack_id: str,
    backend_ref: str,
    environment_ref: str,
    verifier_ref: str,
    grader_ref: str,
    benchmark_case: dict[str, Any],
    native_grader_output: dict[str, Any],
    trace_refs: list[str],
    artifact_refs: list[str],
    failure_class: str = "verification_grading",
) -> dict[str, Any]:
    case = validate_benchmark_adapter_case(benchmark_case)
    grade = _require_mapping(native_grader_output, "native_grader_output")
    verdict = _grader_verdict(grade)
    reason_codes = grade.get("reason_codes", [])
    _require_string_list(reason_codes, "native_grader_output.reason_codes", allow_empty=True)
    contamination_labels = case["contamination_labels"]
    row = {
        "run_id": run_id,
        "eval_id": eval_id,
        "task_pack_id": task_pack_id,
        "family": case["benchmark_family"],
        "surface_type": case["surface_type"],
        "admission_level": case["admission_level"],
        "backend_ref": backend_ref,
        "environment_ref": environment_ref,
        "artifact_refs": artifact_refs,
        "trace_refs": trace_refs,
        "closure_status": "invalid" if verdict == "invalid" else "closed",
        "task_truth_status": verdict,
        "contamination_status": contamination_status_from_labels(contamination_labels),
        "failure_class": "none" if verdict == "pass" else failure_class,
        "reason_codes": reason_codes,
        "verifier_ref": verifier_ref,
        "grader_ref": grader_ref,
        "score": _score_from_grade(grade, verdict),
        "authority_label": case["authority_label"],
        "contamination_labels": contamination_labels,
        "benchmark_case_id": case["benchmark_case_id"],
    }
    row["native_certification_status"] = _native_certification_status(
        authority_label=case["authority_label"],
        admission_level=case["admission_level"],
        backend_ref=backend_ref,
        verdict=verdict,
    )
    row["native_promotion_eligible"] = row["native_certification_status"] == "native_certified_pass"
    return validate_adapter_result_row(row)


def validate_adapter_result_row(row: dict[str, Any]) -> dict[str, Any]:
    data = validate_result_row(row)
    _require_enum(data.get("authority_label"), "result_row.authority_label", ADAPTER_AUTHORITIES)
    labels = _require_string_list(data.get("contamination_labels"), "result_row.contamination_labels")
    expected = contamination_status_from_labels(labels)
    if data["contamination_status"] != expected:
        raise SchemaValidationError("result_row.contamination_status must match contamination_labels")
    return deepcopy(data)


def aggregate_adapter_result_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    totals = {"pass": 0, "fail": 0, "invalid": 0, "total": 0}
    by_authority: dict[str, dict[str, float]] = {}
    scores: list[float] = []
    for row in rows:
        validated = validate_adapter_result_row(row)
        verdict = result_row_verdict(validated)
        authority = validated["authority_label"]
        totals["total"] += 1
        totals[verdict] += 1
        scores.append(float(validated["score"]))
        bucket = by_authority.setdefault(
            authority, {"pass": 0, "fail": 0, "invalid": 0, "total": 0, "score_sum": 0.0}
        )
        bucket["total"] += 1
        bucket[verdict] += 1
        bucket["score_sum"] += float(validated["score"])
    for bucket in by_authority.values():
        count = int(bucket["total"])
        bucket["mean_score"] = bucket["score_sum"] / count if count else 0.0
        del bucket["score_sum"]
    return {
        "row_count": totals["total"],
        "totals": totals,
        "mean_score": sum(scores) / len(scores) if scores else 0.0,
        "by_authority_label": by_authority,
    }


def _validate_execution_unit(unit: dict[str, Any]) -> dict[str, Any]:
    _require_fields(unit, EXECUTION_UNIT_REQUIRED_FIELDS, "execution_unit")
    _require_string(unit["unit_id"], "execution_unit.unit_id")
    _require_string(unit["task_prompt"], "execution_unit.task_prompt")
    if unit["canonical_root"] != "/app":
        raise SchemaValidationError("execution_unit.canonical_root must normalize to /app")
    _require_mapping(unit["execution_contract"], "execution_unit.execution_contract")
    return unit


def _validate_expected_answer(expected: dict[str, Any]) -> dict[str, Any]:
    _require_enum(expected.get("format"), "expected_answer.format", EXPECTED_ANSWER_FORMATS)
    if "value" not in expected:
        raise SchemaValidationError("expected_answer.value is required")
    return expected


def _first_text(output: dict[str, Any]) -> str:
    if isinstance(output.get("assistant_text"), str):
        return output["assistant_text"].strip()
    if isinstance(output.get("final_answer"), str):
        return output["final_answer"].strip()
    if isinstance(output.get("text"), str):
        return output["text"].strip()
    return ""


def _json_from_output(output: dict[str, Any], text: str) -> Any:
    if isinstance(output.get("final_json"), (dict, list)):
        return output["final_json"]
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def _tool_call_sequence(output: dict[str, Any]) -> list[str]:
    calls = output.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [str(item) if isinstance(item, str) else json.dumps(item, sort_keys=True) for item in calls]


def _artifact_ref(output: dict[str, Any]) -> str:
    if isinstance(output.get("artifact_ref"), str):
        return output["artifact_ref"]
    refs = output.get("artifact_refs")
    if isinstance(refs, list) and refs and isinstance(refs[0], str):
        return refs[0]
    return ""


def _grader_verdict(grade: dict[str, Any]) -> str:
    verdict = grade.get("verdict")
    if verdict not in {"pass", "fail", "invalid"}:
        raise SchemaValidationError("native_grader_output.verdict must be pass/fail/invalid")
    return verdict


def _score_from_grade(grade: dict[str, Any], verdict: str) -> float:
    score = grade.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    return 1.0 if verdict == "pass" else 0.0


def _native_certification_status(
    *,
    authority_label: str,
    admission_level: str,
    backend_ref: str,
    verdict: str,
) -> str:
    if authority_label != "native":
        return "equivalent_or_shaped"
    if admission_level != "certified" or not _is_certified_backend(backend_ref):
        return "native_noncertified_context"
    if verdict != "pass":
        return "native_certified_nonpass"
    return "native_certified_pass"


def _is_certified_backend(backend_ref: str) -> bool:
    lowered = backend_ref.strip().lower()
    tokens = ("linux_container", "azure_vm_docker", "docker", "approved_equivalent")
    return any(token in lowered for token in tokens)


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
