"""Model-output parsing for architect configs and solver turns.

Extracted from model_hooks.py for the 500-LOC cap.  Tolerant where the
payload implies intent (fences, prose wrap, missing act label), hard-failing
where inference would fabricate meaning (submission is never inferred).
"""
from __future__ import annotations

import json
import re
from dataclasses import fields as dc_fields
from typing import Any, Mapping

from .runtime_ir import (
    ActionRequest,
    BootstrapPolicy,
    CompletionPolicy,
    ContextPolicy,
    HelperToolPolicy,
    ProcessPolicy,
    ReconfigurePolicy,
    RefusalPolicy,
    RuntimeConfigIR,
    SolverTurn,
    WorkflowPolicy,
)


class _ParseError(Exception):
    pass


def _model_output_error(message: str) -> Exception:
    from .model_hooks import ModelOutputError
    return ModelOutputError(message)


_FENCE_RE = re.compile(r"```(?:json)?\s*\n?", re.IGNORECASE)


def _extract_json_object(text: str) -> str:
    """First balanced ``{...}`` from *text*; tolerates fences and prose."""
    cleaned = _FENCE_RE.sub("", text)
    start = cleaned.find("{")
    if start == -1:
        raise _model_output_error("no JSON object found in model output")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    raise _model_output_error("unbalanced braces in model output")



def _accepted_fields(cls: type) -> set[str]:
    return {f.name for f in dc_fields(cls)}


def _coerce_tuple(value: Any) -> tuple[Any, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, str):
        return (value,)
    return ()


def _build_frozen(cls: type, raw: Mapping[str, Any]) -> Any:
    accepted = _accepted_fields(cls)
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in accepted:
            continue
        field_obj = next(f for f in dc_fields(cls) if f.name == key)
        if field_obj.type in ("tuple[str, ...]", "tuple[str, ...] | None"):
            value = _coerce_tuple(value)
        kwargs[key] = value
    return cls(**kwargs)

_POLICY_MAP: dict[str, type] = {
    "context_policy": ContextPolicy,
    "process_policy": ProcessPolicy,
    "helper_tool_policy": HelperToolPolicy,
    "bootstrap_policy": BootstrapPolicy,
    "completion_policy": CompletionPolicy,
    "refusal_policy": RefusalPolicy,
    "reconfigure_policy": ReconfigurePolicy,
    "workflow_policy": WorkflowPolicy,
}


def parse_runtime_config_ir(text: str, *, capability_index: Any = None) -> RuntimeConfigIR:
    """Parse raw model text into a ``RuntimeConfigIR``."""
    raw_json = _extract_json_object(text)
    try:
        data: dict[str, Any] = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise _model_output_error(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise _model_output_error("expected a JSON object at top level")

    # Required scalars
    if "architect_summary" not in data:
        raise _model_output_error("missing required field: architect_summary")
    if "solver_identity_prompt" not in data:
        raise _model_output_error("missing required field: solver_identity_prompt")

    accepted = _accepted_fields(RuntimeConfigIR)
    kwargs: dict[str, Any] = {}

    for key, value in data.items():
        if key not in accepted:
            continue
        # Policy sub-objects
        if key in _POLICY_MAP and isinstance(value, dict):
            kwargs[key] = _build_frozen(_POLICY_MAP[key], value)
            continue
        # Tuple fields
        field_obj = next((f for f in dc_fields(RuntimeConfigIR) if f.name == key), None)
        if field_obj is not None and "tuple" in str(field_obj.type):
            kwargs[key] = _coerce_tuple(value)
            continue
        kwargs[key] = value

    # selected_capabilities must be a tuple of strings
    if "selected_capabilities" in kwargs:
        kwargs["selected_capabilities"] = tuple(
            str(cap_id) for cap_id in kwargs["selected_capabilities"]
        )
    else:
        kwargs["selected_capabilities"] = ()

    return RuntimeConfigIR(**kwargs)


def parse_solver_turn(text: str) -> SolverTurn:
    """Parse raw model text into a ``SolverTurn``."""
    raw_json = _extract_json_object(text)
    try:
        data: dict[str, Any] = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise _model_output_error(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise _model_output_error("expected a JSON object at top level")

    kind = str(data.get("kind", "")).strip()
    raw_actions = data.get("actions", ()) or ()
    if not kind:
        # A turn that carries actions IS an act turn; do not burn a retry on
        # a missing label the payload already implies.  A turn with neither
        # kind nor actions stays a hard error -- submission is never inferred.
        if isinstance(raw_actions, (list, tuple)) and len(raw_actions) > 0:
            kind = "act"
        else:
            raise _model_output_error("missing required field: kind")

    summary = str(data.get("summary", "")).strip() or f"({kind} turn without summary)"

    actions: list[ActionRequest] = []
    action_accepted = _accepted_fields(ActionRequest)
    for action_index, raw_action in enumerate(raw_actions):
        if not isinstance(raw_action, dict):
            continue
        action_kwargs: dict[str, Any] = {}
        for akey, aval in raw_action.items():
            if akey not in action_accepted:
                continue
            if akey == "arguments" and isinstance(aval, dict):
                action_kwargs[akey] = dict(aval)
            else:
                action_kwargs[akey] = aval
        # Ensure required fields have defaults so construction does not crash
        action_kwargs.setdefault("action_id", f"a-{action_index}")
        if not str(action_kwargs.get("action_id", "")).strip():
            action_kwargs["action_id"] = f"a-{action_index}"
        action_kwargs.setdefault("kind", "")
        action_kwargs.setdefault("capability_id", "")
        action_kwargs.setdefault("arguments", {})
        action_kwargs.setdefault("intent", "")
        action_kwargs.setdefault("expected_observation", "")
        action_kwargs.setdefault("if_fail_next", "")
        actions.append(ActionRequest(**action_kwargs))

    turn_kwargs: dict[str, Any] = {
        "kind": kind,
        "summary": summary,
        "actions": tuple(actions),
    }
    for optional_key in ("requested_check_ids", "claimed_artifacts"):
        if optional_key in data:
            turn_kwargs[optional_key] = _coerce_tuple(data[optional_key])
    if "evidence_gap" in data:
        turn_kwargs["evidence_gap"] = str(data.get("evidence_gap", "")).strip()

    return SolverTurn(**turn_kwargs)
