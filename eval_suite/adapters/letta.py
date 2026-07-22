"""Letta filesystem adapter with native preflight and deterministic grading."""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from eval_suite.adapters.contracts import (
    build_adapter_result_row,
    validate_benchmark_adapter_case,
)
from eval_suite.schemas.eval_substrate_contracts import validate_result_row, validate_task_pack
from eval_suite.adapters.letta_context_bench import (
    LETTA_FILESYSTEM_DATASET,
    LETTA_FILESYSTEM_ROOT,
    letta_preflight,
    grade_letta_filesystem_answer,
    selected_letta_filesystem_specs,
)

ADAPTER_FAMILY = "letta_filesystem_equivalent_adapter"
ADAPTER_AUTHORITY_LABEL = "equivalent"
ADAPTER_AUTHORITY_DETAIL = (
    "letta_filesystem_equivalent_mirrored_dataset_and_deterministic_answer_grader_"
    "not_upstream_rubric_runtime"
)
DEFAULT_CONTAMINATION_LABELS = ["clean", "public_benchmark_row", "mirrored_resource"]
LETTA_SUITE_YAML = LETTA_FILESYSTEM_DATASET
NATIVE_AUTHORITY_LABEL = "native"
NATIVE_AUTHORITY_DETAIL = "letta_native_filesystem_suite_runtime"


def load_selected_cases() -> dict[str, dict[str, Any]]:
    return {
        str(spec["probe_id"]): spec
        for spec in selected_letta_filesystem_specs()
    }


def hidden_truth_ref_for_probe(probe_id: str) -> str:
    return f"hidden://letta-filesystem-equivalent/{probe_id}"


def build_task_pack(*, task_pack_id: str, probe_id: str) -> dict[str, Any]:
    spec = _selected_case(probe_id)
    task_pack = {
        "task_id": task_pack_id,
        "task_prompt": _canonical_prompt(spec["task_prompt"]),
        "fixture": {
            "type": "mirrored_letta_filesystem_case",
            "workspace_ref": "/app/letta/filesystem",
            "dataset_ref": str(LETTA_SUITE_YAML),
            "required_files": list(spec["grade"]["required_files"]),
        },
        "canonical_root": "/app",
        "backend_requirements": {
            "certified_default": "linux_container",
            "debug_backend": "debug_local_no_sandbox",
            "network": "disabled",
        },
        "visible_verifier": {
            "command": f"python3 run_adapter.py --probe-id {probe_id} --workspace /app/letta/filesystem"
        },
        "hidden_verifier": {
            "command_shape": (
                "python3 hidden_grader.py --probe-id <probe_id> --assistant-output <artifact_ref>"
            ),
            "checks_ref": hidden_truth_ref_for_probe(probe_id),
            "leak_hidden_checks_to_prompt": False,
        },
        "grader": {"type": "letta_filesystem_equivalent", "score_range": [0, 1]},
        "contamination_policy": {
            "status": "clean",
            "source": "mirrored_letta_benchmark_resource",
            "public_benchmark_row": True,
        },
        "artifact_capture_policy": {
            "capture": ["environment_manifest", "artifact_bundle", "verifier", "grader", "trace"]
        },
        "admission_level": "diagnostic",
        "surface_type": "filesystem",
        "benchmark_adapter_contract": {
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
            "expected_answer_format": "text",
            "hidden_truth_ref": hidden_truth_ref_for_probe(probe_id),
            "upstream_suite_ref": str(LETTA_SUITE_YAML),
            "upstream_grader_kind": "model_judge",
        },
    }
    return validate_task_pack(task_pack)


def build_benchmark_case(*, probe_id: str, task_pack_id: str) -> dict[str, Any]:
    spec = _selected_case(probe_id)
    benchmark_case = {
        "benchmark_family": ADAPTER_FAMILY,
        "benchmark_case_id": probe_id,
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "surface_type": "filesystem",
        "admission_level": "diagnostic",
        "expected_answer": {
            "format": "text",
            "value": {"hidden_truth_ref": hidden_truth_ref_for_probe(probe_id)},
        },
        "contamination_labels": list(DEFAULT_CONTAMINATION_LABELS),
        "execution_unit": {
            "unit_id": f"{task_pack_id}::{probe_id}",
            "task_prompt": _canonical_prompt(spec["task_prompt"]),
            "canonical_root": "/app",
            "execution_contract": {
                "authority_detail": ADAPTER_AUTHORITY_DETAIL,
                "hidden_truth_ref": hidden_truth_ref_for_probe(probe_id),
                "workspace_ref": "/app/letta/filesystem",
                "required_files": list(spec["grade"]["required_files"]),
            },
        },
    }
    return validate_benchmark_adapter_case(benchmark_case)


def grade_letta_case_equivalent(spec: dict[str, Any], assistant_text: str) -> dict[str, Any]:
    base_grade = grade_letta_filesystem_answer(assistant_text, str(spec["grade"]["ground_truth"]))
    observed = assistant_text.strip()
    truth = str(spec["grade"]["ground_truth"])
    return {
        "verdict": base_grade["verdict"],
        "reason_codes": _reason_codes(base_grade["reason_codes"], observed),
        "score": 1.0 if base_grade["verdict"] == "pass" else 0.0,
        "difficulty": spec["difficulty"],
        "dataset_index": _dataset_index(spec["probe_id"]),
        "question_type": spec["grade"]["question_type"],
        "required_files": list(spec["grade"]["required_files"]),
        "observed_answer_hash": _hash_text(observed),
        "ground_truth_hash": _hash_text(truth),
        "numeric_equivalent": _decimal_text(observed) == _decimal_text(truth),
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "authority_detail": ADAPTER_AUTHORITY_DETAIL,
        "hidden_truth_ref": hidden_truth_ref_for_probe(spec["probe_id"]),
        "upstream_suite_ref": str(LETTA_SUITE_YAML),
        "upstream_grader_kind": "model_judge",
        "adapter_grader_kind": "deterministic_text_numeric_equivalence",
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
    failure_class = "none" if verdict == "pass" else "model_capability"
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
    return validate_result_row(row)


def native_grader_preflight() -> dict[str, Any]:
    details = letta_preflight()
    blockers: list[str] = list(details.get("blockers", []))
    if not LETTA_SUITE_YAML.exists():
        blockers.append("letta_filesystem_dataset_missing")
    try:
        specs = load_selected_cases()
        if not specs:
            blockers.append("empty_letta_case_selection")
    except Exception:
        blockers.append("letta_case_parse_error")
    return {
        "native_runtime_available": not blockers,
        "native_runtime_reason": "available" if not blockers else "blocked",
        "blocker_codes": blockers,
        "suite_yaml": str(LETTA_SUITE_YAML),
        "details": details,
    }


def grade_letta_case_native(spec: dict[str, Any], assistant_text: str) -> dict[str, Any]:
    grade = grade_letta_case_equivalent(spec, assistant_text)
    grade["authority_label"] = NATIVE_AUTHORITY_LABEL
    grade["authority_detail"] = NATIVE_AUTHORITY_DETAIL
    return grade


def _selected_case(probe_id: str) -> dict[str, Any]:
    try:
        return load_selected_cases()[probe_id]
    except KeyError as exc:
        raise ValueError(f"unknown Letta probe_id: {probe_id}") from exc


def _canonical_prompt(task_prompt: str) -> str:
    return task_prompt.replace("./letta/filesystem", "/app/letta/filesystem")


def _dataset_index(probe_id: str) -> int:
    return int(probe_id.split("_")[2])


def _reason_codes(reason_codes: list[str], observed: str) -> list[str]:
    codes = sorted(set(reason_codes))
    if not observed:
        codes.append("letta_no_final_answer")
    return sorted(set(codes))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decimal_text(text: str) -> Decimal | None:
    cleaned = text.replace(",", "")
    matches = re.findall(r"-?\$?\d+(?:\.\d+)?", cleaned)
    if not matches:
        return None
    try:
        return Decimal(matches[-1].replace("$", ""))
    except InvalidOperation:
        return None
