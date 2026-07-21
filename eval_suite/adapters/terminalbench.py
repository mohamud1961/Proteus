"""TerminalBench equivalent adapter for bounded public task slices."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from eval_suite.adapters.contracts import (
    build_adapter_result_row,
    validate_benchmark_adapter_case,
)
from eval_suite.schemas.eval_substrate_contracts import validate_result_row, validate_task_pack
from eval_suite.graders.measurement_contracts import (
    load_financial_document_contract,
    load_regex_log_contract,
)
from eval_suite.graders.measurement_grading import grade_public_terminalbench_workspace
from eval_suite.adapters.terminalbench_paths import resolve_terminalbench_task_root

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_FAMILY = "terminalbench_equivalent_adapter"
ADAPTER_LABEL = "TerminalBench equivalent adapter"
ADAPTER_AUTHORITY_LABEL = "equivalent"
ADAPTER_AUTHORITY_DETAIL = (
    "terminalbench_equivalent_official_task_contract_replay_not_native_runtime"
)
EQUIVALENT_CONTRACT_REPLAY_LABEL = "terminalbench_equivalent_contract_replay"
DEFAULT_CONTAMINATION_LABELS = ["clean", "public_benchmark_row", "mirrored_resource"]
SUPPORTED_TASKS = ("regex-log", "financial-document-processor")
SCHEMA_VERSION = "terminalbench_equivalent_public_task_slice.v1"


def load_selected_cases() -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for task_id in SUPPORTED_TASKS:
        task_root = _task_root(task_id)
        task_meta = _task_meta(task_root / "task.toml")
        prompt = (task_root / "instruction.md").read_text(encoding="utf-8").strip()
        cases[task_id] = {
            "task_id": task_id,
            "probe_id": f"terminalbench_public_{task_id}",
            "difficulty": task_meta["difficulty"],
            "task_prompt": prompt,
            "request_payload": _request_payload_for(task_id, task_meta["difficulty"]),
        }
    return cases


def hidden_truth_ref_for_task(task_id: str) -> str:
    return f"hidden://terminalbench-equivalent/{task_id}"


def verifier_provenance_ref_for_task(task_id: str) -> str:
    return f"provenance://terminalbench-equivalent/{task_id}"


def official_solution_regex(task_id: str) -> str:
    solution = _solution_path(task_id).read_text(encoding="utf-8")
    return solution.split("cat << 'EOF' > /app/regex.txt\n", 1)[1].split("\nEOF", 1)[0]


def build_task_pack(*, task_pack_id: str, task_id: str) -> dict[str, Any]:
    spec = _selected_case(task_id)
    task_pack = {
        "task_id": task_pack_id,
        "task_prompt": spec["task_prompt"],
        "fixture": {
            "type": "mirrored_terminalbench_public_task",
            "workspace_ref": "/app",
            "task_root_ref": str(_task_root(task_id)),
            "request_ref": "/app/request.json",
        },
        "canonical_root": "/app",
        "backend_requirements": {
            "certified_default": "linux_container",
            "debug_backend": "debug_local_no_sandbox",
            "network": "disabled",
        },
        "visible_verifier": {
            "mode": EQUIVALENT_CONTRACT_REPLAY_LABEL,
            "command": f"python3 run_equivalent_contract_replay.py --task-id {task_id} --workspace /app",
            "native_verifier_execution": False,
            "contract_note": "Equivalent contract replay of the mirrored TerminalBench task; not native verifier execution.",
        },
        "hidden_verifier": {
            "mode": "terminalbench_equivalent_hidden_truth_replay",
            "command_shape": "python3 hidden_contract_replay.py --task-id <task_id> --workspace-root /app",
            "checks_ref": hidden_truth_ref_for_task(task_id),
            "leak_hidden_checks_to_prompt": False,
            "native_verifier_execution": False,
            "contract_note": "Equivalent hidden-truth replay for the mirrored task contract; not native verifier execution.",
        },
        "grader": {"type": "terminalbench_equivalent_public_task", "score_range": [0, 1]},
        "contamination_policy": {
            "status": "clean",
            "source": "mirrored_terminalbench_public_task",
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
            "expected_answer_format": "artifact_ref",
            "hidden_truth_ref": hidden_truth_ref_for_task(task_id),
            "verifier_provenance_ref": verifier_provenance_ref_for_task(task_id),
            "source_schema_version": SCHEMA_VERSION,
        },
    }
    return validate_task_pack(task_pack)


def build_benchmark_case(*, task_id: str, task_pack_id: str) -> dict[str, Any]:
    spec = _selected_case(task_id)
    benchmark_case = {
        "benchmark_family": ADAPTER_FAMILY,
        "benchmark_case_id": task_id,
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "surface_type": "filesystem",
        "admission_level": "diagnostic",
        "expected_answer": {
            "format": "artifact_ref",
            "value": {"hidden_truth_ref": hidden_truth_ref_for_task(task_id)},
        },
        "contamination_labels": list(DEFAULT_CONTAMINATION_LABELS),
        "execution_unit": {
            "unit_id": f"{task_pack_id}::{task_id}",
            "task_prompt": spec["task_prompt"],
            "canonical_root": "/app",
            "execution_contract": {
                "authority_detail": ADAPTER_AUTHORITY_DETAIL,
                "hidden_truth_ref": hidden_truth_ref_for_task(task_id),
                "verifier_provenance_ref": verifier_provenance_ref_for_task(task_id),
                "required_artifact_path": spec["request_payload"]["required_artifact_path"],
                "source_schema_version": SCHEMA_VERSION,
            },
        },
    }
    return validate_benchmark_adapter_case(benchmark_case)


def build_verifier_provenance(*, task_id: str) -> dict[str, Any]:
    task_root = _task_root(task_id)
    return {
        "task_id": task_id,
        "task_root_ref": str(task_root),
        "instruction_ref": str(task_root / "instruction.md"),
        "task_toml_ref": str(task_root / "task.toml"),
        "official_verifier_ref": str(task_root / "tests" / "test_outputs.py"),
        "official_solution_ref": str(_solution_path(task_id)),
        "hidden_truth_ref": hidden_truth_ref_for_task(task_id),
        "verifier_provenance_ref": verifier_provenance_ref_for_task(task_id),
        "verifier_mode": EQUIVALENT_CONTRACT_REPLAY_LABEL,
        "source_schema_version": SCHEMA_VERSION,
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "authority_detail": ADAPTER_AUTHORITY_DETAIL,
    }


def build_hidden_truth_payload(*, task_id: str) -> dict[str, Any]:
    if task_id == "regex-log":
        contract = load_regex_log_contract(str(_task_root(task_id)))
        return {
            "task_id": task_id,
            "hidden_truth_ref": hidden_truth_ref_for_task(task_id),
            "verifier_provenance_ref": verifier_provenance_ref_for_task(task_id),
            "expected_dates_hash": _hash_text(json.dumps(contract["expected_dates"], sort_keys=True)),
            "sample_logs_hash": _hash_text(json.dumps(contract["sample_logs"], sort_keys=True)),
            "official_solution_regex_hash": _hash_text(official_solution_regex(task_id)),
            "hidden_truth_fingerprint_sha256": _hash_text(
                json.dumps(
                    {
                        "expected_dates": contract["expected_dates"],
                        "sample_logs": contract["sample_logs"],
                        "solution_regex": official_solution_regex(task_id),
                    },
                    sort_keys=True,
                )
            ),
            "expected_count": len(contract["expected_dates"]),
            "source_schema_version": SCHEMA_VERSION,
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
        }
    if task_id == "financial-document-processor":
        contract = load_financial_document_contract(str(_task_root(task_id)))
        return {
            "task_id": task_id,
            "hidden_truth_ref": hidden_truth_ref_for_task(task_id),
            "verifier_provenance_ref": verifier_provenance_ref_for_task(task_id),
            "invoice_hashes_hash": _hash_text(json.dumps(sorted(contract["invoice_hashes"]), sort_keys=True)),
            "other_hashes_hash": _hash_text(json.dumps(sorted(contract["other_hashes"]), sort_keys=True)),
            "expected_data_hash": _hash_text(json.dumps(contract["expected_data"], sort_keys=True)),
            "summary_total_hash": _hash_text(
                json.dumps(
                    contract["expected_data"]["total"],
                    sort_keys=True,
                )
            ),
            "expected_invoice_count": len(contract["invoice_hashes"]),
            "expected_other_count": len(contract["other_hashes"]),
            "source_schema_version": SCHEMA_VERSION,
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
        }
    raise ValueError(f"unsupported_terminalbench_task:{task_id}")

def grade_terminalbench_case_equivalent(*, task_id: str, workspace: Path) -> dict[str, Any]:
    base = grade_public_terminalbench_workspace(workspace, task_id=task_id)
    if task_id == "regex-log":
        regex_path = workspace / "regex.txt"
        raw_text = regex_path.read_text(encoding="utf-8") if regex_path.exists() else ""
        stripped = raw_text.strip()
        contract = load_regex_log_contract(str(_task_root(task_id)))
        return {
            "verdict": base["verdict"],
            "reason_codes": list(base["reason_codes"]),
            "observed_file_hash": _hash_text(raw_text),
            "normalized_pattern_hash": _hash_text(stripped),
            "expected_dates_hash": _hash_text(json.dumps(contract["expected_dates"], sort_keys=True)),
            "sample_logs_hash": _hash_text(json.dumps(contract["sample_logs"], sort_keys=True)),
            "matched_dates_hash": _hash_text(json.dumps(base.get("matched_dates", []), sort_keys=True)),
            "matched_count": len(base.get("matched_dates", [])),
            "expected_count": len(contract["expected_dates"]),
            "artifact_path": str(regex_path),
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
            "hidden_truth_ref": hidden_truth_ref_for_task(task_id),
            "verifier_provenance_ref": verifier_provenance_ref_for_task(task_id),
            "source_schema_version": SCHEMA_VERSION,
        }
    if task_id == "financial-document-processor":
        summary_path = workspace / "invoices" / "summary.csv"
        summary_text = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        contract = load_financial_document_contract(str(_task_root(task_id)))
        return {
            "verdict": base["verdict"],
            "reason_codes": list(base["reason_codes"]),
            "observed_summary_hash": _hash_text(summary_text),
            "summary_row_count": len(summary_text.splitlines()) - 1 if summary_text else 0,
            "expected_invoice_count": len(contract["invoice_hashes"]),
            "expected_other_count": len(contract["other_hashes"]),
            "artifact_path": str(summary_path),
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
            "hidden_truth_ref": hidden_truth_ref_for_task(task_id),
            "verifier_provenance_ref": verifier_provenance_ref_for_task(task_id),
            "source_schema_version": SCHEMA_VERSION,
        }
    raise ValueError(f"unsupported_terminalbench_task:{task_id}")


def build_result_row_for_grade(
    *,
    run_id: str,
    eval_id: str,
    task_pack_id: str,
    task_id: str,
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
    row = build_adapter_result_row(
        run_id=run_id,
        eval_id=eval_id,
        task_pack_id=task_pack_id,
        backend_ref=backend_ref,
        environment_ref=environment_ref,
        verifier_ref=verifier_ref,
        grader_ref=grader_ref,
        benchmark_case=build_benchmark_case(task_id=task_id, task_pack_id=task_pack_id),
        native_grader_output=grade,
        trace_refs=trace_refs,
        artifact_refs=artifact_refs,
        failure_class="none" if verdict == "pass" else "verification_grading",
    )
    row["control_label"] = control_label
    row["adapter_label"] = ADAPTER_LABEL
    row["authority_detail"] = ADAPTER_AUTHORITY_DETAIL
    row["hidden_truth_ref"] = hidden_truth_ref_for_task(task_id)
    row["verifier_provenance_ref"] = verifier_provenance_ref_for_task(task_id)
    row["source_schema_version"] = SCHEMA_VERSION
    return validate_result_row(row)


def _selected_case(task_id: str) -> dict[str, Any]:
    try:
        return load_selected_cases()[task_id]
    except KeyError as exc:
        raise ValueError(f"unsupported TerminalBench task: {task_id}") from exc


def _task_root(task_id: str) -> Path:
    if task_id not in SUPPORTED_TASKS:
        raise ValueError(f"unsupported TerminalBench task: {task_id}")
    return resolve_terminalbench_task_root(task_id)


def _solution_path(task_id: str) -> Path:
    task_root = _task_root(task_id)
    for relative in (
        Path("solution/solve.sh"),
        Path("solution_solve_sh_reference.txt"),
        Path("solution.sh"),
    ):
        candidate = task_root / relative
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"TerminalBench solution file not found under {task_root}")


def _task_meta(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    difficulty = _quoted_value(text, "difficulty")
    return {"difficulty": difficulty}


def smoke_control_regex(*, task_id: str, control_label: str) -> str:
    if task_id != "regex-log":
        raise ValueError(f"unsupported TerminalBench task: {task_id}")
    if control_label == "pass":
        return official_solution_regex(task_id).replace(r"\d", "[0-9]")
    if control_label == "ceiling":
        return official_solution_regex(task_id)
    if control_label == "known_bad":
        return r"(\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))"
    raise ValueError(f"unsupported control label: {control_label}")


def _request_payload_for(task_id: str, difficulty: str) -> dict[str, Any]:
    if task_id == "regex-log":
        return {
            "task_id": task_id,
            "required_artifact_path": "/app/regex.txt",
            "verifier_ref": "/tests/test_outputs.py",
            "difficulty": difficulty,
        }
    if task_id == "financial-document-processor":
        return {
            "task_id": task_id,
            "required_artifact_path": "/app/invoices/summary.csv",
            "verifier_ref": "/tests/test_outputs.py",
            "difficulty": difficulty,
        }
    raise ValueError(f"unsupported_terminalbench_task:{task_id}")


def _quoted_value(text: str, key: str) -> str:
    marker = f'{key} = "'
    _, _, tail = text.partition(marker)
    if not tail:
        raise ValueError(f"task_toml_key_missing:{key}")
    return tail.split('"', 1)[0]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
