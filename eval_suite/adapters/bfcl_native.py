"""BFCL native adapter using official v3 state-diff grading semantics."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import importlib.util
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from eval_suite.adapters.contracts import (
    build_adapter_result_row,
    validate_benchmark_adapter_case,
)
from eval_suite.adapters import bfcl_assets
from eval_suite.schemas.eval_substrate_contracts import validate_result_row, validate_task_pack

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- Official vendor paths (REAL BFCL v4 assets) ---
OFFICIAL_VENDOR_ROOT = REPO_ROOT / "eval_suite/fixtures/bfcl/official_vendor"
OFFICIAL_VENDOR_FUNC_SOURCE_DIR = OFFICIAL_VENDOR_ROOT / "func_source_code"
OFFICIAL_VENDOR_CHECKER_PATH = OFFICIAL_VENDOR_ROOT / "eval_checker/multi_turn_checker.py"

# --- Legacy paths (NON-OFFICIAL, retained for reference only) ---
_LEGACY_BFCL_SAMPLES_PATH = REPO_ROOT / "eval_suite/fixtures/bfcl/bfcl/benchmark_samples/bfcl_v3_final.json"
_LEGACY_BFCL_APIS_DIR = REPO_ROOT / "eval_suite/fixtures/bfcl/bfcl/bfcl_apis"
OFFICIAL_BFCL_GRADER_SOURCE = OFFICIAL_VENDOR_CHECKER_PATH

ADAPTER_FAMILY = "bfcl_native_adapter"
ADAPTER_LABEL = "BFCL native adapter"
ADAPTER_AUTHORITY_LABEL = "native"
ADAPTER_AUTHORITY_DETAIL = "bfcl_native_official_v3_state_replay_grader_using_official_cases_and_api_assets"
DEFAULT_HIDDEN_CHECKS_REF = "hidden://bfcl-native/official-v3-curated"
DEFAULT_CONTAMINATION_LABELS = ["clean", "public_benchmark_row", "mirrored_resource", "official_subset"]
OFFICIAL_CURATED_CASE_IDS = (
    "multi_turn_base_0",
    "multi_turn_base_1",
    "multi_turn_base_2",
    "multi_turn_miss_func_0",
    "multi_turn_miss_param_0",
)

_CLASS_IMPORTS = {
    "GorillaFileSystem": ("gorilla_file_system", "GorillaFileSystem"),
    "MathAPI": ("math_api", "MathAPI"),
    "MessageAPI": ("message_api", "MessageAPI"),
    "TicketAPI": ("ticket_api", "TicketAPI"),
    "TradingBot": ("trading_bot", "TradingBot"),
    "TravelAPI": ("travel_booking", "TravelAPI"),
    "TwitterAPI": ("posting_api", "TwitterAPI"),
    "VehicleControlAPI": ("vehicle_control", "VehicleControlAPI"),
    "WebSearchAPI": ("web_search", "WebSearchAPI"),
}

_SENDER_ID_SUFFIX = re.compile(r",\s*sender_id=['\"][^'\"]*['\"](?=\s*\))")
_SENDER_ID_PREFIX = re.compile(r"sender_id=['\"][^'\"]*['\"],\s*")


def load_mirrored_cases(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load cases from official vendor data (REAL BFCL v4 multi-turn)."""
    return bfcl_assets.load_official_cases()


def load_official_curated_cases(*, limit: int | None = None) -> dict[str, dict[str, Any]]:
    """Load a curated subset of REAL official BFCL v4 cases with ground truth.

    If limit is set, returns up to that many cases with ground truth.
    Otherwise returns OFFICIAL_CURATED_CASE_IDS if they exist in the data,
    falling back to first N cases with ground truth.
    """
    all_cases = bfcl_assets.load_official_cases()
    if limit is not None:
        result: dict[str, dict[str, Any]] = {}
        for case_id, case in all_cases.items():
            if case.get("ground_truth") and supported_case(case):
                result[case_id] = case
                if len(result) >= limit:
                    break
        return result
    # Try named curated IDs first
    selected = {cid: all_cases.get(cid) for cid in OFFICIAL_CURATED_CASE_IDS}
    available = {cid: c for cid, c in selected.items() if c is not None and c.get("ground_truth")}
    if available:
        return available
    # Fallback: first 5 supported cases with ground truth
    return load_official_curated_cases(limit=5)


def flatten_ground_truth_calls(case: dict[str, Any]) -> list[str]:
    turns = case.get("ground_truth", [])
    calls: list[str] = []
    if not isinstance(turns, list):
        return calls
    for turn in turns:
        if not isinstance(turn, list):
            continue
        for call in turn:
            if isinstance(call, str) and call.strip():
                calls.append(call.strip())
    return calls


def supported_case(case: dict[str, Any]) -> bool:
    classes = case.get("involved_classes", [])
    if not isinstance(classes, list):
        return False
    return all(isinstance(name, str) and name in _CLASS_IMPORTS for name in classes)


def native_grader_preflight() -> dict[str, Any]:
    """Preflight for BFCL native grading.

    Requires REAL official vendor assets. Reports False with
    synthetic_assets_not_official if only synthetic stubs are available.
    """
    asset_preflight = bfcl_assets.bfcl_asset_preflight()
    has_official = asset_preflight.get("official_vendor_available", False)
    official_grader_source_present = OFFICIAL_BFCL_GRADER_SOURCE.exists()
    blocker_codes = list(asset_preflight["blocker_codes"])

    # Detect if legacy synthetic path is the only one available
    if not has_official:
        legacy_sample = asset_preflight.get("selected_sample_path", "")
        if legacy_sample and bfcl_assets._is_synthetic_asset_path(Path(legacy_sample)):
            if "synthetic_assets_not_official" not in blocker_codes:
                blocker_codes.append("synthetic_assets_not_official")

    # Check that func_source_code APIs can actually be imported
    if has_official:
        try:
            _ensure_official_api_import_path()
            test_mod = importlib.import_module("ticket_api")
            if not hasattr(test_mod, "TicketAPI"):
                blocker_codes.append("official_api_import_incomplete")
        except ImportError:
            blocker_codes.append("official_api_import_failed")

    return {
        "native_runtime_available": has_official and not blocker_codes,
        "native_runtime_mode": (
            "official_v4_state_replay" if has_official and not blocker_codes
            else "blocked"
        ),
        "blocker_codes": blocker_codes,
        "official_vendor_available": has_official,
        "official_vendor_root": str(OFFICIAL_VENDOR_ROOT),
        "official_grader_source_present": official_grader_source_present,
        "official_grader_source_ref": str(OFFICIAL_BFCL_GRADER_SOURCE),
    }


def build_task_pack(*, task_pack_id: str, case_id: str) -> dict[str, Any]:
    task_pack = {
        "task_id": task_pack_id,
        "task_prompt": f"Execute official BFCL v3 tool-call sequence for case {case_id}.",
        "fixture": {"type": "official_bfcl_case", "workspace_ref": f"bfcl/{case_id}"},
        "canonical_root": "/app",
        "backend_requirements": {
            "certified_default": "linux_container",
            "debug_backend": "debug_local_no_sandbox",
            "network": "disabled",
        },
        "visible_verifier": {"command": "python3 run_adapter.py --case-id <case_id>"},
        "hidden_verifier": {
            "command_shape": "python3 hidden_grader.py --case-id <case_id> --tool-call-log <artifact_ref>",
            "checks_ref": DEFAULT_HIDDEN_CHECKS_REF,
            "leak_hidden_checks_to_prompt": False,
        },
        "grader": {"type": "bfcl_native_official_state_replay", "score_range": [0, 1]},
        "contamination_policy": {
            "status": "clean",
            "source": "mirrored_official_bfcl_resource",
            "public_benchmark_row": True,
        },
        "artifact_capture_policy": {
            "capture": ["environment_manifest", "artifact_bundle", "verifier", "grader", "trace"]
        },
        "admission_level": "diagnostic",
        "surface_type": "tool_call",
        "benchmark_adapter_contract": {
            "adapter_label": ADAPTER_LABEL,
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
            "expected_answer_format": "tool_call_sequence",
            "hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF,
            "official_grader_source_ref": str(OFFICIAL_BFCL_GRADER_SOURCE),
        },
    }
    return validate_task_pack(task_pack)


def build_benchmark_case(*, case_id: str, task_pack_id: str) -> dict[str, Any]:
    benchmark_case = {
        "benchmark_family": ADAPTER_FAMILY,
        "benchmark_case_id": case_id,
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "surface_type": "tool_call",
        "admission_level": "diagnostic",
        "expected_answer": {
            "format": "tool_call_sequence",
            "value": {"hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF},
        },
        "contamination_labels": list(DEFAULT_CONTAMINATION_LABELS),
        "execution_unit": {
            "unit_id": f"{task_pack_id}::{case_id}",
            "task_prompt": f"Execute official BFCL v3 tool-call sequence for case {case_id}.",
            "canonical_root": "/app",
            "execution_contract": {
                "authority_detail": ADAPTER_AUTHORITY_DETAIL,
                "hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF,
                "official_grader_source_ref": str(OFFICIAL_BFCL_GRADER_SOURCE),
            },
        },
    }
    return validate_benchmark_adapter_case(benchmark_case)


def grade_bfcl_case_native(case: dict[str, Any], observed_calls: list[str]) -> dict[str, Any]:
    expected_calls = flatten_ground_truth_calls(case)
    unsupported = sorted(
        class_name for class_name in case.get("involved_classes", []) if class_name not in _CLASS_IMPORTS
    )
    if unsupported:
        return {
            "verdict": "invalid",
            "reason_codes": ["bfcl_native_unsupported_classes"],
            "unsupported_classes": unsupported,
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
            "hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF,
        }

    gt_instances = _instantiate_case_apis(case)
    observed_instances = _instantiate_case_apis(case)
    expected_errors = _replay_calls(gt_instances, expected_calls, suppress=True)
    observed_errors = _replay_calls(observed_instances, observed_calls, suppress=True)
    mismatch_fields = _state_mismatch_fields(observed_instances, gt_instances, case)

    reason_codes: list[str] = []
    if not observed_calls:
        reason_codes.append("bfcl_no_calls_emitted")
    if mismatch_fields:
        reason_codes.append("bfcl_state_mismatch")

    verdict = "pass" if not reason_codes else "fail"
    return {
        "verdict": verdict,
        "reason_codes": sorted(set(reason_codes)),
        "score": 1.0 if verdict == "pass" else 0.0,
        "expected_call_count": len(expected_calls),
        "observed_call_count": len(observed_calls),
        "expected_calls_hash": _hash_list([_normalize_call(call) for call in expected_calls]),
        "observed_calls_hash": _hash_list([_normalize_call(call) for call in observed_calls]),
        "expected_replay_error_hash": _hash_list(expected_errors),
        "observed_replay_error_hash": _hash_list(observed_errors),
        "expected_replay_error_count": len(expected_errors),
        "observed_replay_error_count": len(observed_errors),
        "state_mismatch_field_count": len(mismatch_fields),
        "state_mismatch_fields": mismatch_fields[:50],
        "state_mismatch_hash": _hash_list(mismatch_fields),
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "authority_detail": ADAPTER_AUTHORITY_DETAIL,
        "hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF,
        "official_grader_source_ref": str(OFFICIAL_BFCL_GRADER_SOURCE),
    }


def build_result_row_for_grade(
    *,
    run_id: str,
    eval_id: str,
    task_pack_id: str,
    case_id: str,
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
        benchmark_case=build_benchmark_case(case_id=case_id, task_pack_id=task_pack_id),
        native_grader_output=grade,
        trace_refs=trace_refs,
        artifact_refs=artifact_refs,
        failure_class="none" if verdict == "pass" else "tool_contract",
    )
    row["control_label"] = control_label
    row["adapter_label"] = ADAPTER_LABEL
    row["authority_detail"] = ADAPTER_AUTHORITY_DETAIL
    row["hidden_truth_ref"] = DEFAULT_HIDDEN_CHECKS_REF
    return validate_result_row(row)


def _instantiate_case_apis(case: dict[str, Any]) -> dict[str, Any]:
    _ensure_official_api_import_path()
    instances: dict[str, Any] = {}
    for class_name in case.get("involved_classes", []):
        module_name, symbol = _CLASS_IMPORTS[class_name]
        module = importlib.import_module(module_name)
        cls = getattr(module, symbol)
        instance = cls()
        instance._load_scenario(copy.deepcopy(case.get("initial_config", {}).get(class_name, {})), long_context=False)
        instances[class_name] = instance
    return instances


def _replay_calls(instances: dict[str, Any], calls: list[str], *, suppress: bool) -> list[str]:
    methods: dict[str, Any] = {}
    for instance in instances.values():
        for method_name in instance.__class__.__dict__:
            if method_name.startswith("_"):
                continue
            method = getattr(instance, method_name, None)
            if callable(method):
                methods[method_name] = method
    errors: list[str] = []
    for index, raw_call in enumerate(calls):
        try:
            func, args, kwargs = _parse_safe_call_expression(_fix_bfcl_gt_call(raw_call))
            method = methods.get(func)
            if method is None:
                raise ValueError(f"unknown_method:{func}")
            method(*args, **kwargs)
        except Exception as exc:
            errors.append(f"{index}:{type(exc).__name__}")
            if not suppress:
                continue
    return errors


def _state_mismatch_fields(observed_instances: dict[str, Any], expected_instances: dict[str, Any], case: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for class_name in case.get("involved_classes", []):
        observed_inst = observed_instances[class_name]
        expected_inst = expected_instances[class_name]
        for attr_name in vars(expected_inst):
            if attr_name.startswith("_"):
                continue
            if _to_jsonable(getattr(observed_inst, attr_name)) != _to_jsonable(getattr(expected_inst, attr_name)):
                mismatches.append(f"{class_name}.{attr_name}")
    return mismatches


def _normalize_call(raw_call: str) -> str:
    stripped = _fix_bfcl_gt_call(raw_call.strip())
    try:
        parsed = ast.parse(stripped, mode="eval")
    except SyntaxError:
        return re.sub(r"\s+", " ", stripped)
    return ast.dump(parsed.body, annotate_fields=True, include_attributes=False)


def _hash_list(items: list[str]) -> str:
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()


def _fix_bfcl_gt_call(call_str: str) -> str:
    call_str = _SENDER_ID_SUFFIX.sub("", call_str)
    return _SENDER_ID_PREFIX.sub("", call_str)


def _ensure_official_api_import_path() -> None:
    """Add official vendor paths to sys.path for API class imports.

    Adds func_source_code/ for direct class imports (e.g. 'import ticket_api')
    and the vendor root for bfcl_eval package shim resolution.
    """
    func_dir = str(OFFICIAL_VENDOR_FUNC_SOURCE_DIR)
    vendor_root = str(OFFICIAL_VENDOR_ROOT)
    if func_dir not in sys.path:
        sys.path.insert(0, func_dir)
    if vendor_root not in sys.path:
        sys.path.insert(0, vendor_root)


# Legacy alias
_ensure_mirrored_api_import_path = _ensure_official_api_import_path


def _parse_safe_call_expression(raw_call: str) -> tuple[str, list[Any], dict[str, Any]]:
    parsed = ast.parse(raw_call.strip(), mode="eval")
    if not isinstance(parsed.body, ast.Call):
        raise ValueError("unsupported_expression")
    call = parsed.body
    if not isinstance(call.func, ast.Name):
        raise ValueError("unsupported_function_reference")
    args = [ast.literal_eval(node) for node in call.args]
    kwargs: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ValueError("star_kwargs_not_supported")
        kwargs[str(keyword.arg)] = ast.literal_eval(keyword.value)
    return call.func.id, args, kwargs


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_jsonable(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_to_jsonable(item) for item in value)
    return value
