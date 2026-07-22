"""TaskContract: pure-model contract extraction from task prompts.

Frozen dataclasses representing the verifiable contract a task implies,
plus a system prompt for the contract-architect model call and a parser
that tolerates typical model output quirks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from aether_next.model_hooks import ModelOutputError, _extract_json_object
from aether_next.runtime_ir import WORKFLOW_MODES, normalize_relpath


# ---------------------------------------------------------------------------
# Contract dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractDeliverable:
    path: str
    kind: str = "file"
    description: str = ""
    required: bool = True


@dataclass(frozen=True)
class ContractSchema:
    target: str
    required_keys: tuple[str, ...] = ()
    value_types: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContractThreshold:
    name: str
    comparator: str  # ">=", ">", "<=", "<", "=="
    target: float
    unit: str = ""
    source: str = ""


@dataclass(frozen=True)
class ContractCheck:
    kind: str  # "file_exists", "json_parses", "schema_keys", "file_size", "command"
    target: str = ""
    command: str = ""
    detail: str = ""


@dataclass(frozen=True)
class TaskContract:
    task_understanding: str
    deliverables: tuple[ContractDeliverable, ...]
    constraints: tuple[str, ...] = ()
    things_to_preserve: tuple[str, ...] = ()
    output_schemas: tuple[ContractSchema, ...] = ()
    thresholds: tuple[ContractThreshold, ...] = ()
    required_checks: tuple[ContractCheck, ...] = ()
    workflow: str = "direct_build"
    capabilities: tuple[str, ...] = ()
    tooling_notes: tuple[str, ...] = ()
    success_definition: str = ""
    stop_conditions: tuple[str, ...] = ()
    failure_hypotheses: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SORTED_WORKFLOWS = sorted(WORKFLOW_MODES)

CONTRACT_ARCHITECT_SYSTEM_PROMPT = f"""\
You are the Contract Architect.  Given JSON with keys task_prompt, envmap \
(visible_files etc.), and capability_index, extract the task's verifiable \
contract.

Read ONLY the task_prompt and visible files.  You must NEVER assume hidden \
tests exist or guess grader logic.

Emit ONLY strict JSON (no markdown fences, no prose) matching these keys:

  task_understanding    (str) one-paragraph summary of what the task asks
  deliverables          (list[object]) every output file the task asks you to \
create/write/produce — use absolute paths like /app/x.ext
    Each object: {{path, kind ("file"|"directory"), description, required (bool)}}
  constraints           (list[str]) hard constraints from the prompt
  things_to_preserve    (list[str]) input files/paths that must NOT be modified
  output_schemas        (list[object]) when the prompt enumerates fields of a \
JSON/CSV output, capture {{target: "<path>", required_keys: [...], \
value_types: {{key: type_str}}}}
  thresholds            (list[object]) size/accuracy/count limits — each \
{{name, comparator (">="|">"|"<="|"<"|"=="), target (number), unit, source}}
    For file size limits use source="file_size_bytes:<path>"
    For accuracy/metric limits use source="accuracy" or source="metric"
  required_checks       (list[object]) checks the task explicitly names — each \
{{kind ("file_exists"|"json_parses"|"schema_keys"|"file_size"|"command"), \
target, command, detail}}
  workflow              (str) one of {_SORTED_WORKFLOWS}
  capabilities          (list[str]) capability_id strings from capability_index
  tooling_notes         (list[str]) tools/libs the task mentions
  success_definition    (str) one sentence defining "done"
  stop_conditions       (list[str]) concrete conditions under which to submit
  failure_hypotheses    (list[str]) likely failure modes (e.g. \
"dependency_missing", "git_permissions", "large_file", "timeout")

Strict JSON only.  No commentary outside the object."""


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _coerce_tuple_str(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if isinstance(value, str):
        return (value,)
    return ()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_deliverable(raw: Any, workspace_root: str) -> ContractDeliverable | None:
    if not isinstance(raw, dict):
        return None
    path = normalize_relpath(str(raw.get("path", "")), workspace_root)
    if not path or path == ".":
        return None
    return ContractDeliverable(
        path=path,
        kind=str(raw.get("kind", "file")),
        description=str(raw.get("description", "")),
        required=bool(raw.get("required", True)),
    )


def _parse_schema(raw: Any, workspace_root: str) -> ContractSchema | None:
    if not isinstance(raw, dict):
        return None
    target = normalize_relpath(str(raw.get("target", "")), workspace_root)
    if not target or target == ".":
        return None
    keys = _coerce_tuple_str(raw.get("required_keys", ()))
    vtypes = raw.get("value_types", {})
    if not isinstance(vtypes, dict):
        vtypes = {}
    return ContractSchema(
        target=target,
        required_keys=keys,
        value_types={str(k): str(v) for k, v in vtypes.items()},
    )


def _parse_threshold(raw: Any) -> ContractThreshold | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", "")).strip()
    if not name:
        return None
    comparator = str(raw.get("comparator", ">="))
    if comparator not in (">=", ">", "<=", "<", "=="):
        comparator = ">="
    return ContractThreshold(
        name=name,
        comparator=comparator,
        target=_coerce_float(raw.get("target", 0)),
        unit=str(raw.get("unit", "")),
        source=str(raw.get("source", "")),
    )


def _parse_check(raw: Any) -> ContractCheck | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", "")).strip()
    valid_kinds = {"file_exists", "json_parses", "schema_keys", "file_size", "command"}
    if kind not in valid_kinds:
        return None
    return ContractCheck(
        kind=kind,
        target=str(raw.get("target", "")),
        command=str(raw.get("command", "")),
        detail=str(raw.get("detail", "")),
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_task_contract(
    raw: str,
    *,
    workspace_root: str = "/app",
) -> TaskContract:
    """Parse raw model text into a ``TaskContract``.

    Raises ``ModelOutputError`` on unrecoverable shape problems.
    """
    json_str = _extract_json_object(raw)
    try:
        data: dict[str, Any] = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ModelOutputError("expected a JSON object at top level")

    task_understanding = str(data.get("task_understanding", "")).strip()
    if not task_understanding:
        raise ModelOutputError("missing required field: task_understanding")

    # Deliverables
    raw_deliverables = data.get("deliverables", [])
    if not isinstance(raw_deliverables, list):
        raw_deliverables = []
    deliverables: list[ContractDeliverable] = []
    for item in raw_deliverables:
        parsed = _parse_deliverable(item, workspace_root)
        if parsed is not None:
            deliverables.append(parsed)

    # Output schemas
    raw_schemas = data.get("output_schemas", [])
    if not isinstance(raw_schemas, list):
        raw_schemas = []
    schemas: list[ContractSchema] = []
    for item in raw_schemas:
        parsed_s = _parse_schema(item, workspace_root)
        if parsed_s is not None:
            schemas.append(parsed_s)

    # Thresholds
    raw_thresholds = data.get("thresholds", [])
    if not isinstance(raw_thresholds, list):
        raw_thresholds = []
    thresholds: list[ContractThreshold] = []
    for item in raw_thresholds:
        parsed_t = _parse_threshold(item)
        if parsed_t is not None:
            thresholds.append(parsed_t)

    # Required checks
    raw_checks = data.get("required_checks", [])
    if not isinstance(raw_checks, list):
        raw_checks = []
    checks: list[ContractCheck] = []
    for item in raw_checks:
        parsed_c = _parse_check(item)
        if parsed_c is not None:
            checks.append(parsed_c)

    # Workflow
    workflow = str(data.get("workflow", "direct_build"))
    if workflow not in WORKFLOW_MODES:
        workflow = "direct_build"

    return TaskContract(
        task_understanding=task_understanding,
        deliverables=tuple(deliverables),
        constraints=_coerce_tuple_str(data.get("constraints", ())),
        things_to_preserve=tuple(
            normalize_relpath(p, workspace_root)
            for p in _coerce_tuple_str(data.get("things_to_preserve", ()))
            if normalize_relpath(p, workspace_root)
        ),
        output_schemas=tuple(schemas),
        thresholds=tuple(thresholds),
        required_checks=tuple(checks),
        workflow=workflow,
        capabilities=_coerce_tuple_str(data.get("capabilities", ())),
        tooling_notes=_coerce_tuple_str(data.get("tooling_notes", ())),
        success_definition=str(data.get("success_definition", "")),
        stop_conditions=_coerce_tuple_str(data.get("stop_conditions", ())),
        failure_hypotheses=_coerce_tuple_str(data.get("failure_hypotheses", ())),
    )
