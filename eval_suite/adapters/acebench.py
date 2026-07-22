"""ACEBench adapter with native-first grading and equivalent fallback."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from eval_suite.adapters.contracts import build_adapter_result_row, validate_benchmark_adapter_case
from eval_suite.schemas.eval_substrate_contracts import validate_result_row, validate_task_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_FAMILY = "acebench_adapter"
NATIVE_ADAPTER_LABEL = "ACEBench native adapter"
EQUIVALENT_ADAPTER_LABEL = "ACEBench equivalent adapter"
NATIVE_AUTHORITY_LABEL = "native"
EQUIVALENT_AUTHORITY_LABEL = "equivalent"
NATIVE_AUTHORITY_DETAIL = "acebench_native_official_eval_main_runtime_and_checker"
EQUIVALENT_AUTHORITY_DETAIL = "acebench_equivalent_curated_checker_not_official_eval_main"
DEFAULT_CONTAMINATION_LABELS = ["clean", "public_benchmark_row", "mirrored_resource", "official_subset"]
SAMPLE_CATEGORY = "normal_atom_bool"
SAMPLE_CASE_ID = "normal_atom_bool_0"
SCHEMA_VERSION = "acebench_adapter_foundation.v1"
SELECTED_CASE_SPECS: dict[str, dict[str, Any]] = {
    "normal_atom_bool_0": {
        "case_id": "normal_atom_bool_0",
        "category": "normal_atom_bool",
        "task_prompt": (
            "Return a single tool call for the user request using the provided function schema. "
            "Do not include prose."
        ),
        "expected_call": {
            "ProteinRichMealPlanner_generateList": {
                "meal_type": "dinner",
                "include_vegetarian_options": True,
                "cuisine_preference": "Asian",
            }
        },
    },
    "normal_atom_enum_0": {
        "case_id": "normal_atom_enum_0",
        "category": "normal_atom_enum",
        "task_prompt": (
            "Return a single tool call for the user request using the provided function schema. "
            "Do not include prose."
        ),
        "expected_call": {
            "StockInsightProvider_getTechStockInsights": {
                "region": "North America",
                "analysisType": "Technical",
                "dataSource": "Bloomberg",
            }
        },
    },
    "normal_atom_object_deep_0": {
        "case_id": "normal_atom_object_deep_0",
        "category": "normal_atom_object_deep",
        "task_prompt": (
            "Return a single tool call for the user request using the provided function schema. "
            "Do not include prose."
        ),
        "expected_call": {
            "partner_assessment_evaluate_operational_efficiency": {
                "partner_id": "TP-12345",
                "evaluation_criteria": {
                    "metrics": ["time savings", "cost reduction"],
                    "time_frame": "Last Quarter",
                },
            }
        },
    },
}


def hidden_truth_ref_for_case(case_id: str, authority_label: str) -> str:
    return f"hidden://acebench/{authority_label}/{case_id}"


def provenance_ref_for_case(case_id: str, authority_label: str) -> str:
    return f"provenance://acebench/{authority_label}/{case_id}"


def selected_case_spec(case_id: str = SAMPLE_CASE_ID) -> dict[str, Any]:
    try:
        return dict(SELECTED_CASE_SPECS[case_id])
    except KeyError as exc:
        raise ValueError(f"unknown ACEBench case_id: {case_id}") from exc


def build_native_tool_definitions(
    *,
    case_id: str = SAMPLE_CASE_ID,
    upstream_root: Path | None = None,
) -> list[dict[str, Any]]:
    spec = selected_case_spec(case_id)
    root = upstream_root or _resolve_upstream_root()
    prompt_path = root / f"data_all/data_en/data_{spec['category']}.json"
    if not prompt_path.exists():
        return []
    rows = _load_acebench_prompt_rows(prompt_path)
    row = next((item for item in rows if str(item.get("id", "")) == case_id), None)
    if not isinstance(row, dict):
        return []
    functions = row.get("function", [])
    if not isinstance(functions, list):
        return []
    definitions: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in functions:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in seen_names:
            continue
        definition = _normalize_acebench_tool_definition(entry)
        definitions.append(definition)
        seen_names.add(name)
    return definitions


def build_task_pack(
    *,
    task_pack_id: str,
    authority_label: str,
    authority_detail: str,
    adapter_label: str,
    case_id: str = SAMPLE_CASE_ID,
    admission_level: str = "diagnostic",
) -> dict[str, Any]:
    spec = selected_case_spec(case_id)
    task_pack = {
        "task_id": task_pack_id,
        "task_prompt": spec["task_prompt"],
        "fixture": {
            "type": "acebench_single_case",
            "workspace_ref": "/app/acebench",
            "case_id": spec["case_id"],
            "category": spec["category"],
            "request_ref": "/app/acebench/request.json",
        },
        "canonical_root": "/app",
        "backend_requirements": {
            "certified_default": "linux_container",
            "debug_backend": "debug_local_no_sandbox",
            "network": "disabled",
        },
        "visible_verifier": {
            "command": "python3 run_adapter.py --case-id <case_id>",
            "native_verifier_execution": authority_label == NATIVE_AUTHORITY_LABEL,
        },
        "hidden_verifier": {
            "command_shape": "python3 hidden_grader.py --case-id <case_id> --artifact <artifact_ref>",
            "checks_ref": hidden_truth_ref_for_case(spec["case_id"], authority_label),
            "leak_hidden_checks_to_prompt": False,
            "native_verifier_execution": authority_label == NATIVE_AUTHORITY_LABEL,
        },
        "grader": {"type": "acebench_adapter", "score_range": [0, 1]},
        "contamination_policy": {
            "status": "clean",
            "source": "mirrored_or_external_acebench",
            "public_benchmark_row": True,
        },
        "artifact_capture_policy": {
            "capture": ["environment_manifest", "artifact_bundle", "verifier", "grader", "trace"]
        },
        "admission_level": admission_level,
        "surface_type": "tool_call",
        "benchmark_adapter_contract": {
            "adapter_label": adapter_label,
            "authority_label": authority_label,
            "authority_detail": authority_detail,
            "expected_answer_format": "tool_call_sequence",
            "hidden_truth_ref": hidden_truth_ref_for_case(spec["case_id"], authority_label),
            "row_provenance_ref": provenance_ref_for_case(spec["case_id"], authority_label),
            "source_schema_version": SCHEMA_VERSION,
        },
    }
    return validate_task_pack(task_pack)


def build_benchmark_case(
    *,
    task_pack_id: str,
    authority_label: str,
    authority_detail: str,
    case_id: str = SAMPLE_CASE_ID,
    admission_level: str = "diagnostic",
) -> dict[str, Any]:
    spec = selected_case_spec(case_id)
    benchmark_case = {
        "benchmark_family": ADAPTER_FAMILY,
        "benchmark_case_id": spec["case_id"],
        "authority_label": authority_label,
        "surface_type": "tool_call",
        "admission_level": admission_level,
        "expected_answer": {
            "format": "tool_call_sequence",
            "value": {
                "hidden_truth_ref": hidden_truth_ref_for_case(spec["case_id"], authority_label),
            },
        },
        "contamination_labels": list(DEFAULT_CONTAMINATION_LABELS),
        "execution_unit": {
            "unit_id": f"{task_pack_id}::{spec['case_id']}",
            "task_prompt": spec["task_prompt"],
            "canonical_root": "/app",
            "execution_contract": {
                "authority_detail": authority_detail,
                "hidden_truth_ref": hidden_truth_ref_for_case(spec["case_id"], authority_label),
                "row_provenance_ref": provenance_ref_for_case(spec["case_id"], authority_label),
                "source_schema_version": SCHEMA_VERSION,
            },
        },
    }
    return validate_benchmark_adapter_case(benchmark_case)


def native_grader_preflight(
    *,
    upstream_root: Path | None = None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    root = upstream_root or _resolve_upstream_root()
    py_candidates = [python_executable] if python_executable else []
    py_candidates.extend([os.environ.get("ACEBENCH_NATIVE_PYTHON"), "python3", "python3.12", "python3.11"])
    py_candidates = [candidate for candidate in py_candidates if candidate]

    required_paths = {
        "eval_main": root / "eval_main.py",
        "prompt_data": root / f"data_all/data_en/data_{SAMPLE_CATEGORY}.json",
        "possible_answer_data": root / f"data_all/data_en/possible_answer/data_{SAMPLE_CATEGORY}.json",
    }
    missing_paths = [name for name, path in required_paths.items() if not path.exists()]

    selected_python = ""
    python_check = {"import_ok": False, "stderr_tail": "", "exit_code": None}
    for candidate in py_candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import pandas,openpyxl; print('ok')"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except FileNotFoundError:
            python_check = {
                "import_ok": False,
                "stderr_tail": f"{candidate}: not found",
                "exit_code": None,
            }
            continue
        if probe.returncode == 0 and "ok" in probe.stdout:
            selected_python = candidate
            python_check = {"import_ok": True, "stderr_tail": "", "exit_code": 0}
            break
        python_check = {
            "import_ok": False,
            "stderr_tail": (probe.stderr or probe.stdout)[-1000:],
            "exit_code": probe.returncode,
        }

    blockers: list[str] = []
    if missing_paths:
        blockers.append("missing_upstream_assets")
    if not python_check["import_ok"]:
        blockers.append("missing_runtime_dependencies")

    return {
        "native_runtime_available": not blockers,
        "native_runtime_reason": "available" if not blockers else "blocked",
        "blocker_codes": blockers,
        "upstream_root": str(root),
        "python_executable": selected_python,
        "python_probe": python_check,
        "missing_paths": missing_paths,
    }


def grade_case_equivalent(observed_text: str, *, case_id: str = SAMPLE_CASE_ID) -> dict[str, Any]:
    expected_call = selected_case_spec(case_id)["expected_call"]
    parsed_calls, parse_error = _decode_tool_calls(observed_text)
    reason_codes: list[str] = []

    if parse_error:
        reason_codes.append("acebench_output_decode_failure")
    if not parsed_calls:
        reason_codes.append("acebench_no_calls_emitted")
    observed_call = parsed_calls[0] if parsed_calls else {}
    if observed_call and list(observed_call.keys()) != list(expected_call.keys()):
        reason_codes.append("acebench_function_name_mismatch")
    if observed_call and not _deep_equal(observed_call, expected_call):
        reason_codes.append("acebench_parameter_mismatch")

    verdict = "pass" if not reason_codes else "fail"
    return {
        "verdict": verdict,
        "reason_codes": sorted(set(reason_codes)),
        "score": 1.0 if verdict == "pass" else 0.0,
        "expected_call_hash": _hash_json(expected_call),
        "observed_call_hash": _hash_json(observed_call),
        "observed_answer_hash": _hash_text(observed_text.strip()),
        "parse_error": parse_error,
        "authority_label": EQUIVALENT_AUTHORITY_LABEL,
        "authority_detail": EQUIVALENT_AUTHORITY_DETAIL,
        "hidden_truth_ref": hidden_truth_ref_for_case(case_id, EQUIVALENT_AUTHORITY_LABEL),
    }


def grade_case_native(
    *,
    observed_text: str,
    upstream_root: Path,
    python_executable: str,
    case_id: str = SAMPLE_CASE_ID,
) -> dict[str, Any]:
    spec = selected_case_spec(case_id)
    category = spec["category"]
    model_name = f"acebench_native_adapter_{next(tempfile._get_candidate_names())}"

    prompt_path = upstream_root / f"data_all/data_en/data_{category}.json"
    possible_path = upstream_root / f"data_all/data_en/possible_answer/data_{category}.json"
    prompts = [json.loads(line) for line in prompt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    possible = [json.loads(line) for line in possible_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(prompts) != len(possible):
        raise ValueError("acebench_prompt_possible_answer_length_mismatch")

    result_lines: list[dict[str, Any]] = []
    for row in possible:
        row_id = str(row.get("id", ""))
        result_text = observed_text if row_id == case_id else _render_from_ground_truth(row.get("ground_truth"))
        result_lines.append({"id": row_id, "result": result_text})

    result_dir = upstream_root / "result_all/result_en" / model_name
    score_dir = upstream_root / "score_all/score_en" / model_name
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f"data_{category}_result.json"
    result_path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in result_lines) + "\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [
            python_executable,
            "eval_main.py",
            "--language",
            "en",
            "--model",
            model_name,
            "--category",
            category,
        ],
        cwd=str(upstream_root),
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout)[-2000:])

    score_path = score_dir / f"data_{category}_score.json"
    score_rows = [json.loads(line) for line in score_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    fail_rows = [row for row in score_rows[1:] if isinstance(row, dict)]
    fail_ids = {str(row.get("id", "")) for row in fail_rows}
    target_fail = next((row for row in fail_rows if str(row.get("id", "")) == case_id), None)

    verdict = "fail" if case_id in fail_ids else "pass"
    reason_codes: list[str] = []
    if target_fail is not None:
        error_type = target_fail.get("error_type")
        if isinstance(error_type, str) and error_type:
            reason_codes.append(f"acebench_{error_type}")
        if not reason_codes:
            reason_codes.append("acebench_native_checker_rejected")

    return {
        "verdict": verdict,
        "reason_codes": reason_codes,
        "score": 1.0 if verdict == "pass" else 0.0,
        "observed_answer_hash": _hash_text(observed_text.strip()),
        "expected_call_hash": _hash_json(spec["expected_call"]),
        "native_score_metadata": score_rows[0] if score_rows else {},
        "native_score_path": str(score_path),
        "native_result_path": str(result_path),
        "native_stdout_tail": cp.stdout[-2000:],
        "authority_label": NATIVE_AUTHORITY_LABEL,
        "authority_detail": NATIVE_AUTHORITY_DETAIL,
        "hidden_truth_ref": hidden_truth_ref_for_case(case_id, NATIVE_AUTHORITY_LABEL),
    }


def build_result_row_for_grade(
    *,
    run_id: str,
    eval_id: str,
    task_pack_id: str,
    control_label: str,
    environment_ref: str,
    artifact_refs: list[str],
    trace_refs: list[str],
    verifier_ref: str,
    grader_ref: str,
    grade: dict[str, Any],
    authority_label: str,
    authority_detail: str,
    case_id: str = SAMPLE_CASE_ID,
    backend_ref: str = "debug_local_no_sandbox",
    admission_level: str = "diagnostic",
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
        benchmark_case=build_benchmark_case(
            task_pack_id=task_pack_id,
            authority_label=authority_label,
            authority_detail=authority_detail,
            case_id=case_id,
            admission_level=admission_level,
        ),
        native_grader_output=grade,
        trace_refs=trace_refs,
        artifact_refs=artifact_refs,
        failure_class="none" if verdict == "pass" else "verification_grading",
    )
    row["control_label"] = control_label
    row["authority_detail"] = authority_detail
    row["adapter_label"] = (
        NATIVE_ADAPTER_LABEL if authority_label == NATIVE_AUTHORITY_LABEL else EQUIVALENT_ADAPTER_LABEL
    )
    row["hidden_truth_ref"] = hidden_truth_ref_for_case(case_id, authority_label)
    row["row_provenance_ref"] = provenance_ref_for_case(case_id, authority_label)
    row["source_schema_version"] = SCHEMA_VERSION
    return validate_result_row(row)


def _render_from_ground_truth(ground_truth: Any) -> str:
    payload = ground_truth[0] if isinstance(ground_truth, list) and ground_truth else ground_truth
    if not isinstance(payload, dict) or len(payload) != 1:
        return "[]"
    function_name, args = next(iter(payload.items()))
    if not isinstance(args, dict):
        return f"[{function_name}()]"
    rendered_args = ", ".join(f"{key}={_to_python_literal(value)}" for key, value in args.items())
    return f"[{function_name}({rendered_args})]"


def _decode_tool_calls(text: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        parsed = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        return [], f"{type(exc).__name__}:{exc.msg}"
    if not isinstance(parsed.body, ast.List):
        return [], "not_list_output"

    calls: list[dict[str, Any]] = []
    for elem in parsed.body.elts:
        if not isinstance(elem, ast.Call):
            return [], "list_contains_non_call"
        function_name = _call_name(elem.func)
        args: dict[str, Any] = {}
        for keyword in elem.keywords:
            if keyword.arg is None:
                return [], "star_kwargs_not_supported"
            args[keyword.arg] = ast.literal_eval(keyword.value)
        calls.append({function_name: args})
    return calls, None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    raise ValueError("unsupported_call_name_node")


def _deep_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False
    )


def _to_python_literal(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_to_python_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [f"{repr(key)}: {_to_python_literal(item)}" for key, item in value.items()]
        return "{" + ", ".join(parts) + "}"
    return repr(value)


def _resolve_upstream_root() -> Path:
    from_env = os.environ.get("ACEBENCH_UPSTREAM_ROOT")
    if from_env:
        return Path(from_env).resolve()
    # Prefer /private/tmp canonical location; fall back to user home if set.
    # Set ACEBENCH_UPSTREAM_ROOT env var to override for all environments.
    candidates = (
        Path("/private/tmp/acebench_upstream"),
        Path.home() / "acebench_upstream",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _load_acebench_prompt_rows(prompt_path: Path) -> list[dict[str, Any]]:
    text = prompt_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "tasks", "examples", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _normalize_acebench_tool_definition(entry: dict[str, Any]) -> dict[str, Any]:
    schema = entry.get("parameters") if isinstance(entry.get("parameters"), dict) else entry.get("arguments")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        description = f"ACEBench function {entry.get('name', '')}"
    return {
        "name": entry["name"],
        "description": description,
        "parameters": dict(schema),
        "input_schema": dict(schema),
    }


def _hash_json(payload: Any) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
