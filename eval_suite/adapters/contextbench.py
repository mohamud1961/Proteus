"""ContextBench adapter with native preflight and deterministic row grading."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from eval_suite.adapters.contracts import (
    build_adapter_result_row,
    validate_benchmark_adapter_case,
)
from eval_suite.schemas.eval_substrate_contracts import validate_result_row, validate_task_pack
from eval_suite.graders.measurement_grading import grade_contextbench_verified_answer

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXTBENCH_ROOT = REPO_ROOT / "research/sources/codebases/ContextBench"
_DEFAULT_VERIFIED_CSV_PATH = CONTEXTBENCH_ROOT / "data/Verified.csv"
_FIXTURE_VERIFIED_CSV_PATH = REPO_ROOT / "eval_suite/fixtures/contextbench/contextbench/Verified.csv"
_FALLBACK_VERIFIED_CSV_PATH = REPO_ROOT / "work/ledger/final_harness_eval_suite/adapter_fixtures/contextbench/Verified.csv"
if _DEFAULT_VERIFIED_CSV_PATH.exists():
    VERIFIED_CSV_PATH = _DEFAULT_VERIFIED_CSV_PATH
elif _FIXTURE_VERIFIED_CSV_PATH.exists():
    VERIFIED_CSV_PATH = _FIXTURE_VERIFIED_CSV_PATH
else:
    VERIFIED_CSV_PATH = _FALLBACK_VERIFIED_CSV_PATH
ADAPTER_FAMILY = "contextbench_equivalent_adapter"
ADAPTER_AUTHORITY_LABEL = "equivalent"
ADAPTER_AUTHORITY_DETAIL = (
    "contextbench_equivalent_verified_csv_structured_row_grader_"
    "not_upstream_contextbench_runtime"
)
DEFAULT_CONTAMINATION_LABELS = ["clean", "public_benchmark_row", "mirrored_resource"]
NATIVE_AUTHORITY_LABEL = "native"
NATIVE_AUTHORITY_DETAIL = "contextbench_native_verified_csv_runtime"
DEFAULT_SELECTED_CASE_COUNT = 8
SCHEMA_VERSION = "contextbench_equivalent_selected_row.v1"
EXPECTED_KEYS = (
    "original_inst_id",
    "language",
    "status",
    "gold_context_length",
    "commit",
    "repo_or_file_family",
)


def load_selected_cases(limit: int = DEFAULT_SELECTED_CASE_COUNT) -> dict[str, dict[str, Any]]:
    rows = list(csv.DictReader(VERIFIED_CSV_PATH.read_text(encoding="utf-8").splitlines()))[:limit]
    cases = {}
    for index, row in enumerate(rows):
        probe_id = f"contextbench_verified_{index:02d}"
        cases[probe_id] = {
            "probe_id": probe_id,
            "dataset_index": index,
            "task_id": row["instance_id"],
            "task_prompt": _canonical_prompt(),
            "request_payload": {
                "instance_id": row["instance_id"],
                "required_keys": list(EXPECTED_KEYS),
                "dataset_index": index,
            },
            "expected_answer_payload": expected_answer_payload(row),
            "grade_row": row,
        }
    return cases


def hidden_truth_ref_for_probe(probe_id: str) -> str:
    return f"hidden://contextbench-equivalent/{probe_id}"


def row_provenance_ref_for_probe(probe_id: str) -> str:
    return f"provenance://contextbench-equivalent/{probe_id}"


def build_task_pack(*, task_pack_id: str, probe_id: str) -> dict[str, Any]:
    spec = _selected_case(probe_id)
    task_pack = {
        "task_id": task_pack_id,
        "task_prompt": spec["task_prompt"],
        "fixture": {
            "type": "mirrored_contextbench_verified_row",
            "workspace_ref": "/app/contextbench",
            "dataset_ref": str(VERIFIED_CSV_PATH),
            "request_ref": "/app/contextbench/request.json",
        },
        "canonical_root": "/app",
        "backend_requirements": {
            "certified_default": "linux_container",
            "debug_backend": "debug_local_no_sandbox",
            "network": "disabled",
        },
        "visible_verifier": {
            "command": (
                f"python3 run_adapter.py --probe-id {probe_id} "
                "--dataset /app/contextbench/Verified.csv --request /app/contextbench/request.json"
            )
        },
        "hidden_verifier": {
            "command_shape": (
                "python3 hidden_grader.py --probe-id <probe_id> --assistant-output <artifact_ref>"
            ),
            "checks_ref": hidden_truth_ref_for_probe(probe_id),
            "leak_hidden_checks_to_prompt": False,
        },
        "grader": {"type": "contextbench_equivalent_structured_row", "score_range": [0, 1]},
        "contamination_policy": {
            "status": "clean",
            "source": "mirrored_contextbench_verified_csv",
            "public_benchmark_row": True,
        },
        "artifact_capture_policy": {
            "capture": ["environment_manifest", "artifact_bundle", "verifier", "grader", "trace"]
        },
        "admission_level": "diagnostic",
        "surface_type": "retrieval",
        "benchmark_adapter_contract": {
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
            "expected_answer_format": "json",
            "hidden_truth_ref": hidden_truth_ref_for_probe(probe_id),
            "row_provenance_ref": row_provenance_ref_for_probe(probe_id),
            "source_schema_version": SCHEMA_VERSION,
        },
    }
    return validate_task_pack(task_pack)


def build_benchmark_case(*, probe_id: str, task_pack_id: str) -> dict[str, Any]:
    spec = _selected_case(probe_id)
    benchmark_case = {
        "benchmark_family": ADAPTER_FAMILY,
        "benchmark_case_id": spec["task_id"],
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "surface_type": "retrieval",
        "admission_level": "diagnostic",
        "expected_answer": {
            "format": "json",
            "value": {
                "hidden_truth_ref": hidden_truth_ref_for_probe(probe_id),
                "required_keys": list(EXPECTED_KEYS),
            },
        },
        "contamination_labels": list(DEFAULT_CONTAMINATION_LABELS),
        "execution_unit": {
            "unit_id": f"{task_pack_id}::{probe_id}",
            "task_prompt": spec["task_prompt"],
            "canonical_root": "/app",
            "execution_contract": {
                "authority_detail": ADAPTER_AUTHORITY_DETAIL,
                "hidden_truth_ref": hidden_truth_ref_for_probe(probe_id),
                "row_provenance_ref": row_provenance_ref_for_probe(probe_id),
                "request_ref": "/app/contextbench/request.json",
                "source_schema_version": SCHEMA_VERSION,
            },
        },
    }
    return validate_benchmark_adapter_case(benchmark_case)


def build_row_provenance(*, probe_id: str) -> dict[str, Any]:
    spec = _selected_case(probe_id)
    row = spec["grade_row"]
    return {
        "probe_id": probe_id,
        "benchmark_case_id": spec["task_id"],
        "dataset_index": spec["dataset_index"],
        "dataset_ref": str(VERIFIED_CSV_PATH),
        "request_payload": spec["request_payload"],
        "row_provenance_ref": row_provenance_ref_for_probe(probe_id),
        "hidden_truth_ref": hidden_truth_ref_for_probe(probe_id),
        "row_fingerprint_sha256": _hash_mapping(expected_answer_payload(row)),
        "source_row_schema_version": SCHEMA_VERSION,
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "authority_detail": ADAPTER_AUTHORITY_DETAIL,
    }


def expected_answer_payload(row: dict[str, str]) -> dict[str, str]:
    return {
        "original_inst_id": str(row["original_inst_id"]),
        "language": str(row["language"]),
        "status": str(row["status"]),
        "gold_context_length": str(row["gold_context_length"]),
        "commit": str(row["commit"]),
        "repo_or_file_family": str(row["original_inst_id"]).split("__", 1)[0],
    }


def grade_contextbench_case_equivalent(spec: dict[str, Any], assistant_text: str) -> dict[str, Any]:
    base_grade = grade_contextbench_verified_answer(assistant_text, spec["grade_row"])
    parsed = base_grade.get("parsed_answer", {})
    reason_codes = list(base_grade["reason_codes"])
    if not assistant_text.strip():
        reason_codes.append("contextbench_no_final_answer")
    if not parsed:
        reason_codes.append("contextbench_structured_answer_missing")
    verdict = "pass" if base_grade["verdict"] == "pass" else "fail"
    return {
        "verdict": verdict,
        "reason_codes": [] if verdict == "pass" else sorted(set(reason_codes)),
        "matched_fields": base_grade["matched_fields"],
        "observed_answer_hash": _hash_text(assistant_text.strip()),
        "observed_structured_hash": _hash_mapping(parsed),
        "expected_row_hash": _hash_mapping(spec["expected_answer_payload"]),
        "dataset_index": spec["dataset_index"],
        "required_keys": list(EXPECTED_KEYS),
        "source_schema_version": SCHEMA_VERSION,
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "authority_detail": ADAPTER_AUTHORITY_DETAIL,
        "hidden_truth_ref": hidden_truth_ref_for_probe(spec["probe_id"]),
        "row_provenance_ref": row_provenance_ref_for_probe(spec["probe_id"]),
        "source_dataset_ref": str(VERIFIED_CSV_PATH),
    }


def build_result_row_for_grade(
    *,
    run_id: str,
    eval_id: str,
    task_pack_id: str,
    probe_id: str,
    control_label: str,
    environment_ref: str,
    artifact_refs: list[str],
    trace_refs: list[str],
    verifier_ref: str,
    grader_ref: str,
    grade: dict[str, Any],
    backend_ref: str = "debug_local_no_sandbox",
) -> dict[str, Any]:
    verdict = str(grade["verdict"])
    failure_class = "none" if verdict == "pass" else "reduction_selection"
    row = build_adapter_result_row(
        run_id=run_id,
        eval_id=eval_id,
        task_pack_id=task_pack_id,
        backend_ref=backend_ref,
        environment_ref=environment_ref,
        verifier_ref=verifier_ref,
        grader_ref=grader_ref,
        benchmark_case=build_benchmark_case(probe_id=probe_id, task_pack_id=task_pack_id),
        native_grader_output=grade,
        trace_refs=trace_refs,
        artifact_refs=artifact_refs,
        failure_class=failure_class,
    )
    row["control_label"] = control_label
    row["authority_detail"] = ADAPTER_AUTHORITY_DETAIL
    row["hidden_truth_ref"] = hidden_truth_ref_for_probe(probe_id)
    row["row_provenance_ref"] = row_provenance_ref_for_probe(probe_id)
    row["source_schema_version"] = SCHEMA_VERSION
    return validate_result_row(row)


def native_grader_preflight() -> dict[str, Any]:
    blockers: list[str] = []
    if not VERIFIED_CSV_PATH.exists():
        blockers.append("missing_verified_csv")
    else:
        try:
            rows = load_selected_cases()
            if not rows:
                blockers.append("empty_verified_csv_selection")
        except Exception:
            blockers.append("verified_csv_parse_error")
    return {
        "native_runtime_available": not blockers,
        "native_runtime_reason": "available" if not blockers else "blocked",
        "blocker_codes": blockers,
        "verified_csv_path": str(VERIFIED_CSV_PATH),
    }


def grade_contextbench_case_native(spec: dict[str, Any], assistant_text: str) -> dict[str, Any]:
    grade = grade_contextbench_case_equivalent(spec, assistant_text)
    grade["authority_label"] = NATIVE_AUTHORITY_LABEL
    grade["authority_detail"] = NATIVE_AUTHORITY_DETAIL
    return grade


def _selected_case(probe_id: str) -> dict[str, Any]:
    try:
        return load_selected_cases()[probe_id]
    except KeyError as exc:
        raise ValueError(f"unknown ContextBench probe_id: {probe_id}") from exc


def _canonical_prompt() -> str:
    return (
        "Read /contextbench/request.json and /contextbench/Verified.csv, find the requested row by "
        "instance_id, and return a JSON object with exactly these keys: original_inst_id, language, "
        "status, gold_context_length, commit, repo_or_file_family."
    )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_mapping(payload: dict[str, Any]) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True))
