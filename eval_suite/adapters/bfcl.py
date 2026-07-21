"""BFCL-equivalent benchmark adapter using mirrored fixtures and grader logic."""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import importlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from eval_suite.adapters.contracts import (
    build_adapter_result_row,
    validate_benchmark_adapter_case,
)
from eval_suite.adapters import bfcl_assets
from eval_suite.schemas.eval_substrate_contracts import validate_result_row, validate_task_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BFCL_SAMPLES_PATH = REPO_ROOT / "eval_suite/fixtures/bfcl/bfcl/benchmark_samples/bfcl_v3_final.json"
_DEFAULT_BFCL_APIS_DIR = REPO_ROOT / "eval_suite/fixtures/bfcl/bfcl/bfcl_apis"
_FALLBACK_BFCL_SAMPLES_PATH = REPO_ROOT / "research/sources/codebases/deepagents/libs/evals/tests/evals/data/benchmark_samples/bfcl_v3_final.json"
_FALLBACK_BFCL_APIS_DIR = REPO_ROOT / "research/sources/codebases/deepagents/libs/evals/tests/evals/data/bfcl_apis"
MIRRORED_BFCL_SAMPLES_PATH = _DEFAULT_BFCL_SAMPLES_PATH if _DEFAULT_BFCL_SAMPLES_PATH.exists() else _FALLBACK_BFCL_SAMPLES_PATH
MIRRORED_BFCL_APIS_DIR = _DEFAULT_BFCL_APIS_DIR if _DEFAULT_BFCL_APIS_DIR.exists() else _FALLBACK_BFCL_APIS_DIR
ADAPTER_FAMILY = "bfcl_equivalent_adapter"
ADAPTER_AUTHORITY_LABEL = "equivalent"
ADAPTER_AUTHORITY_DETAIL = "bfcl_equivalent_mirrored_data_and_grader_not_official_runtime"
DEFAULT_HIDDEN_CHECKS_REF = "hidden://bfcl-equivalent/mirrored-v3"
DEFAULT_CONTAMINATION_LABELS = ["clean", "public_benchmark_row", "mirrored_resource"]

_CLASS_IMPORTS = {
    "MessageAPI": ("bfcl_apis.message_api", "MessageAPI"),
    "TicketAPI": ("bfcl_apis.ticket_api", "TicketAPI"),
    "TradingBot": ("bfcl_apis.trading_bot", "TradingBot"),
    "TravelAPI": ("bfcl_apis.travel_booking", "TravelAPI"),
    "VehicleControlAPI": ("bfcl_apis.vehicle_control", "VehicleControlAPI"),
}

_SENDER_ID_SUFFIX = re.compile(r",\s*sender_id=['\"][^'\"]*['\"](?=\s*\))")
_SENDER_ID_PREFIX = re.compile(r"sender_id=['\"][^'\"]*['\"],\s*")


def load_mirrored_cases(path: Path | None = None) -> dict[str, dict[str, Any]]:
    if path is None:
        path = bfcl_assets.resolve_bfcl_samples_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("mirrored BFCL sample payload must be a list or dict with tasks")
    return {str(case["id"]): case for case in cases if isinstance(case, dict) and "id" in case}


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


def build_native_tool_definitions(case: dict[str, Any]) -> list[dict[str, Any]]:
    if not supported_case(case):
        return []
    _ensure_mirrored_api_import_path()
    definitions: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for class_name in case.get("involved_classes", []):
        if not isinstance(class_name, str) or class_name not in _CLASS_IMPORTS:
            continue
        module_name, symbol = _CLASS_IMPORTS[class_name]
        module = importlib.import_module(module_name)
        cls = getattr(module, symbol)
        for method_name, member in cls.__dict__.items():
            if method_name.startswith("_") or not callable(member):
                continue
            if method_name in seen_names:
                continue
            definitions.append(
                _tool_definition_from_callable(
                    class_name=class_name,
                    tool_name=method_name,
                    callable_obj=member,
                    case=case,
                )
            )
            seen_names.add(method_name)
    return definitions


def build_task_pack(*, task_pack_id: str, case_id: str) -> dict[str, Any]:
    task_pack = {
        "task_id": task_pack_id,
        "task_prompt": f"Execute BFCL-equivalent tool-call sequence for mirrored case {case_id}.",
        "fixture": {"type": "mirrored_bfcl_case", "workspace_ref": f"bfcl/{case_id}"},
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
        "grader": {"type": "bfcl_equivalent_mirrored", "score_range": [0, 1]},
        "contamination_policy": {
            "status": "clean",
            "source": "mirrored_bfcl_resource",
            "public_benchmark_row": True,
        },
        "artifact_capture_policy": {
            "capture": ["environment_manifest", "artifact_bundle", "verifier", "grader", "trace"]
        },
        "admission_level": "diagnostic",
        "surface_type": "tool_call",
        "benchmark_adapter_contract": {
            "authority_label": ADAPTER_AUTHORITY_LABEL,
            "authority_detail": ADAPTER_AUTHORITY_DETAIL,
            "expected_answer_format": "tool_call_sequence",
            "hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF,
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
            "task_prompt": f"Execute BFCL-equivalent tool-call sequence for mirrored case {case_id}.",
            "canonical_root": "/app",
            "execution_contract": {
                "authority_detail": ADAPTER_AUTHORITY_DETAIL,
                "hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF,
            },
        },
    }
    return validate_benchmark_adapter_case(benchmark_case)


def grade_bfcl_case_equivalent(case: dict[str, Any], observed_calls: list[str]) -> dict[str, Any]:
    expected_calls = flatten_ground_truth_calls(case)
    expected_raw_hash = _hash_calls(expected_calls)
    observed_raw_hash = _hash_calls(observed_calls)
    normalized_expected = [_normalize_call(call) for call in expected_calls]
    normalized_observed = [_normalize_call(call) for call in observed_calls]
    call_match = normalized_expected == normalized_observed
    expected_hash = _hash_calls(normalized_expected)
    observed_hash = _hash_calls(normalized_observed)
    unsupported = sorted(
        class_name
        for class_name in case.get("involved_classes", [])
        if class_name not in _CLASS_IMPORTS
    )
    state_match = False
    observed_errors: list[str] = []
    expected_errors: list[str] = []

    if not unsupported:
        expected_instances = _instantiate_case_apis(case)
        observed_instances = _instantiate_case_apis(case)
        expected_errors = _replay_calls(expected_instances, expected_calls)
        observed_errors = _replay_calls(observed_instances, observed_calls)
        state_match = _snapshot_state(expected_instances) == _snapshot_state(observed_instances)

    reason_codes: list[str] = []
    if unsupported:
        reason_codes.append("bfcl_mirrored_unsupported_classes")
    if not observed_calls:
        reason_codes.append("bfcl_no_calls_emitted")
    if len(normalized_observed) < len(normalized_expected):
        reason_codes.append("bfcl_missing_required_calls")
    if len(normalized_observed) > len(normalized_expected):
        reason_codes.append("bfcl_extra_calls_emitted")
    if not call_match:
        reason_codes.append("bfcl_order_or_arguments_mismatch")
    replay_errors_diverged = observed_errors != expected_errors
    if replay_errors_diverged:
        reason_codes.append("bfcl_observed_call_execution_error")
    if not unsupported and not state_match:
        reason_codes.append("bfcl_state_divergence_after_replay")

    verdict = "invalid" if unsupported else ("pass" if not reason_codes else "fail")
    return {
        "verdict": verdict,
        "reason_codes": sorted(set(reason_codes)),
        "expected_call_count": len(expected_calls),
        "observed_call_count": len(observed_calls),
        "expected_raw_calls_hash": expected_raw_hash,
        "observed_raw_calls_hash": observed_raw_hash,
        "expected_calls_hash": expected_hash,
        "observed_calls_hash": observed_hash,
        "first_mismatch_index": _first_mismatch_index(normalized_expected, normalized_observed),
        "call_match": call_match,
        "state_match": state_match if not unsupported else None,
        "unsupported_classes": unsupported,
        "observed_replay_error_count": len(observed_errors),
        "expected_replay_error_count": len(expected_errors),
        "observed_replay_error_hash": _hash_calls(observed_errors),
        "expected_replay_error_hash": _hash_calls(expected_errors),
        "authority_label": ADAPTER_AUTHORITY_LABEL,
        "authority_detail": ADAPTER_AUTHORITY_DETAIL,
        "hidden_truth_ref": DEFAULT_HIDDEN_CHECKS_REF,
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
    failure_class = "none" if verdict == "pass" else ("unclear" if verdict == "invalid" else "tool_contract")
    benchmark_case = build_benchmark_case(case_id=case_id, task_pack_id=task_pack_id)
    row = build_adapter_result_row(
        run_id=run_id,
        eval_id=eval_id,
        task_pack_id=task_pack_id,
        backend_ref=backend_ref,
        environment_ref=environment_ref,
        verifier_ref=verifier_ref,
        grader_ref=grader_ref,
        benchmark_case=benchmark_case,
        native_grader_output=grade,
        trace_refs=trace_refs,
        artifact_refs=artifact_refs,
        failure_class=failure_class,
    )
    row["control_label"] = control_label
    row["authority_detail"] = ADAPTER_AUTHORITY_DETAIL
    row["hidden_truth_ref"] = DEFAULT_HIDDEN_CHECKS_REF
    return validate_result_row(row)


def _tool_definition_from_callable(
    *,
    class_name: str,
    tool_name: str,
    callable_obj: Any,
    case: dict[str, Any],
) -> dict[str, Any]:
    description = _tool_description(class_name=class_name, tool_name=tool_name, callable_obj=callable_obj)
    parameters = _tool_parameters_from_callable(callable_obj)
    return {
        "name": tool_name,
        "description": description,
        "parameters": parameters,
        "input_schema": dict(parameters),
        "runtime_spec": _runtime_spec_from_callable(
            class_name=class_name,
            tool_name=tool_name,
            case=case,
        ),
    }


def _tool_description(*, class_name: str, tool_name: str, callable_obj: Any) -> str:
    doc = inspect.getdoc(callable_obj) or ""
    first_line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first_line or f"{class_name}.{tool_name}"


def _tool_parameters_from_callable(callable_obj: Any) -> dict[str, Any]:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):  # pragma: no cover - defensive fallback
        return {"type": "object", "properties": {}, "additionalProperties": True}

    properties: dict[str, Any] = {}
    required: list[str] = []
    allow_additional_properties = False
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls"}:
            continue
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            allow_additional_properties = True
            continue
        properties[parameter.name] = _schema_from_annotation(parameter.annotation)
        if parameter.default is inspect._empty:
            required.append(parameter.name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": allow_additional_properties or False,
    }
    if required:
        schema["required"] = required
    return schema


def _runtime_spec_from_callable(*, class_name: str, tool_name: str, case: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime_kind": "bfcl_api_method",
        "module_name": _CLASS_IMPORTS[class_name][0],
        "class_name": class_name,
        "method_name": tool_name,
        "import_root": str(MIRRORED_BFCL_APIS_DIR.parent),
        "initial_config": copy.deepcopy(case.get("initial_config", {}).get(class_name, {})),
        "long_context": False,
    }


def _schema_from_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect._empty or annotation is Any:
        return {"type": "object", "additionalProperties": True}
    if annotation in {str}:
        return {"type": "string"}
    if annotation in {int}:
        return {"type": "integer"}
    if annotation in {float}:
        return {"type": "number"}
    if annotation in {bool}:
        return {"type": "boolean"}
    if annotation in {dict,}:
        return {"type": "object", "additionalProperties": True}
    if annotation in {list, tuple, set, frozenset}:
        return {"type": "array", "items": {"type": "object", "additionalProperties": True}}
    if annotation is type(None):
        return {"type": "null"}

    origin = get_origin(annotation)
    if origin in {list, tuple, set, frozenset}:
        args = get_args(annotation)
        item_schema = _schema_from_annotation(args[0]) if args else {"type": "object", "additionalProperties": True}
        return {"type": "array", "items": item_schema}
    if origin in {dict}:
        args = get_args(annotation)
        value_schema = _schema_from_annotation(args[1]) if len(args) > 1 else {"type": "object", "additionalProperties": True}
        return {"type": "object", "additionalProperties": value_schema}
    if origin in {types.UnionType, Union}:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if not args:
            return {"type": "null"}
        if len(args) == 1:
            return _schema_from_annotation(args[0])
        return {"anyOf": [_schema_from_annotation(arg) for arg in args]}
    if origin is Literal:
        values = list(get_args(annotation))
        if values:
            return {"enum": values}
    return {"type": "object", "additionalProperties": True}


def _normalize_call(raw_call: str) -> str:
    stripped = _SENDER_ID_PREFIX.sub("", _SENDER_ID_SUFFIX.sub("", raw_call.strip()))
    try:
        parsed = ast.parse(stripped, mode="eval")
    except SyntaxError:
        return re.sub(r"\s+", " ", stripped)
    if not isinstance(parsed.body, ast.Call):
        return ast.dump(parsed.body, annotate_fields=True, include_attributes=False)
    call = parsed.body
    filtered_keywords = [kw for kw in call.keywords if kw.arg != "sender_id"]
    filtered_keywords = sorted(filtered_keywords, key=lambda kw: kw.arg or "")
    canonical = ast.Call(func=call.func, args=call.args, keywords=filtered_keywords)
    return ast.dump(canonical, annotate_fields=True, include_attributes=False)


def _hash_calls(normalized_calls: list[str]) -> str:
    return hashlib.sha256("\n".join(normalized_calls).encode("utf-8")).hexdigest()


def _ensure_mirrored_api_import_path() -> None:
    parent = str(bfcl_assets.resolve_bfcl_apis_dir().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)


def _instantiate_case_apis(case: dict[str, Any]) -> dict[str, Any]:
    _ensure_mirrored_api_import_path()
    instances: dict[str, Any] = {}
    for class_name in case.get("involved_classes", []):
        module_name, symbol = _CLASS_IMPORTS[class_name]
        module = importlib.import_module(module_name)
        cls = getattr(module, symbol)
        instance = cls()
        initial_config = copy.deepcopy(case.get("initial_config", {}).get(class_name, {}))
        instance._load_scenario(initial_config, long_context=False)
        instances[class_name] = instance
    return instances


def _replay_calls(instances: dict[str, Any], calls: list[str]) -> list[str]:
    bound: dict[str, Any] = {}
    for instance in instances.values():
        for name in instance.__class__.__dict__:
            if name.startswith("_"):
                continue
            method = getattr(instance, name, None)
            if callable(method):
                bound[name] = method
    errors: list[str] = []
    for index, call in enumerate(calls):
        try:
            function_name, args, kwargs = _parse_safe_call_expression(call)
            method = bound.get(function_name)
            if method is None:
                raise ValueError(f"unknown_method:{function_name}")
            method(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - exercised by known-bad controls
            errors.append(f"{index}:{type(exc).__name__}")
    return errors


def _snapshot_state(instances: dict[str, Any]) -> dict[str, Any]:
    return {name: _to_jsonable(_public_attrs(instance)) for name, instance in instances.items()}


def _public_attrs(instance: Any) -> dict[str, Any]:
    return {name: value for name, value in vars(instance).items() if not name.startswith("_")}


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


def _first_mismatch_index(expected: list[str], observed: list[str]) -> int | None:
    for index, (left, right) in enumerate(zip(expected, observed)):
        if left != right:
            return index
    if len(expected) != len(observed):
        return min(len(expected), len(observed))
    return None


def _parse_safe_call_expression(raw_call: str) -> tuple[str, list[Any], dict[str, Any]]:
    parsed = ast.parse(raw_call.strip(), mode="eval")
    if not isinstance(parsed.body, ast.Call):
        raise ValueError("unsupported_expression")
    call = parsed.body
    if not isinstance(call.func, ast.Name):
        raise ValueError("unsupported_function_reference")
    if call.starargs is not None if hasattr(call, "starargs") else False:  # pragma: no cover
        raise ValueError("star_args_not_supported")
    if call.kwargs is not None if hasattr(call, "kwargs") else False:  # pragma: no cover
        raise ValueError("star_kwargs_not_supported")
    args: list[Any] = []
    for node in call.args:
        args.append(ast.literal_eval(node))
    kwargs: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ValueError("star_kwargs_not_supported")
        kwargs[str(keyword.arg)] = ast.literal_eval(keyword.value)
    return call.func.id, args, kwargs
