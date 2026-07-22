"""Contract-to-IR compilation: turn a TaskContract into harness structures.

Pure functions that bridge the model-extracted TaskContract to the existing
ObjectiveGraph, EvalIndex, and RuntimeConfigIR without modifying those types.
"""
from __future__ import annotations

import os
import shlex
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Mapping

from aether_next.runtime_ir import (
    WORKFLOW_MODES,
    CheckSpec,
    DeliverableSpec,
    EvalIndex,
    MetricThreshold,
    ObjectiveGraph,
    ProcessPolicy,
    ProofObligation,
    RuntimeConfigIR,
    WorkflowPolicy,
    normalize_relpath,
)
from .task_contract import (
    ContractCheck,
    ContractSchema,
    ContractThreshold,
    TaskContract,
)

if TYPE_CHECKING:
    from .compiler import ConfigCompiler
    from aether_next.repair import repair_config as _repair_sig  # noqa: F401
    from .runtime_ir import EnvMap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_id(origin: str, command: str) -> str:
    """Stable check-id from origin + command, matching analysis._check_id."""
    digest = sha256(f"{origin}|{command}".encode("utf-8")).hexdigest()[:10]
    return f"check-{digest}"



def _mb_to_bytes(mb: float) -> int:
    return int(mb * 1024 * 1024)


def _comparator_to_test_op(comparator: str) -> str:
    """Map a threshold comparator to a shell `test` numeric operator."""
    return {
        "<": "-lt",
        "<=": "-le",
        ">": "-gt",
        ">=": "-ge",
        "==": "-eq",
    }.get(comparator, "-lt")


def _schema_check_command(target: str, required_keys: tuple[str, ...]) -> str:
    """Return a deterministic schema check for the target file format."""
    target_repr = repr(target)
    keys_repr = repr(list(required_keys))
    if target.lower().endswith(".csv"):
        script = (
            "import csv; "
            f"keys={keys_repr}; "
            f"fh=open({target_repr}, newline=''); "
            "reader=csv.DictReader(fh); "
            "fields=reader.fieldnames or []; "
            "assert all(k in fields for k in keys)"
        )
    else:
        script = (
            "import json; "
            f"d=json.load(open({target_repr})); "
            f"assert all(k in d for k in {keys_repr})"
        )
    return "python3 -c " + shlex.quote(script)


# ---------------------------------------------------------------------------
# ObjectiveGraph from contract
# ---------------------------------------------------------------------------

def contract_to_objective_graph(
    contract: TaskContract,
    envmap: Mapping[str, Any] | Any,
) -> ObjectiveGraph:
    """Build an ObjectiveGraph from a TaskContract."""
    workspace_root: str = ""
    if hasattr(envmap, "workspace_root"):
        workspace_root = envmap.workspace_root

    # Deliverables — skip test paths.
    deliverables: list[DeliverableSpec] = []
    seen_paths: set[str] = set()
    for cd in contract.deliverables:
        path = normalize_relpath(cd.path, workspace_root)
        if not path or path.startswith("tests/") or path in seen_paths:
            continue
        seen_paths.add(path)
        deliverables.append(
            DeliverableSpec(
                path=path,
                required=cd.required,
                description=cd.description,
            )
        )

    # Protected paths
    protected: list[str] = []
    seen_protected: set[str] = set()
    for raw in contract.things_to_preserve:
        path = normalize_relpath(raw, workspace_root)
        if path and path not in seen_protected:
            seen_protected.add(path)
            protected.append(path)

    # Thresholds -- only include locally measurable thresholds (those whose
    # source starts with "file_size_bytes:") in the objective graph.
    # Non-measurable thresholds (accuracy, rsa_bits, etc.) remain in the
    # contract for solver visibility but must NOT become gate-blocking
    # MetricThresholds, otherwise the gate blocks on missing_metric forever.
    thresholds = tuple(
        MetricThreshold(
            name=ct.name,
            comparator=ct.comparator,
            target=ct.target,
        )
        for ct in contract.thresholds
        if ct.source.startswith("file_size_bytes:")
    )

    # Output schema — pick the first schema whose target matches a deliverable,
    # or fall back to the first schema.
    output_schema: dict[str, str] = {}
    output_schema_target: str = ""
    if contract.output_schemas:
        deliverable_paths = {d.path for d in deliverables}
        chosen: ContractSchema | None = None
        for schema in contract.output_schemas:
            norm_target = normalize_relpath(schema.target, workspace_root)
            if norm_target in deliverable_paths:
                chosen = schema
                break
        if chosen is None:
            chosen = contract.output_schemas[0]
        norm_chosen_target = normalize_relpath(chosen.target, workspace_root)
        output_schema = {k: str(chosen.value_types.get(k, "any")) for k in chosen.required_keys}
        output_schema_target = norm_chosen_target

    # Obligations
    obligations: list[ProofObligation] = []
    for d in deliverables:
        if d.required:
            obligations.append(
                ProofObligation(
                    obligation_id=f"artifact:{d.path}",
                    kind="artifact",
                    description=f"required artifact {d.path}",
                    target=d.path,
                )
            )
    obligations.append(
        ProofObligation(
            obligation_id="integrity:clean",
            kind="integrity",
            description="no protected or disallowed edits",
            target="clean_workspace",
        )
    )

    return ObjectiveGraph(
        deliverables=tuple(deliverables),
        protected_paths=tuple(protected),
        allowed_edit_roots=(".",),
        service_requirements=(),
        package_requirements=(),
        thresholds=thresholds,
        output_schema=output_schema,
        output_schema_target=output_schema_target,
        obligations=tuple(obligations),
    )


# ---------------------------------------------------------------------------
# EvalIndex from contract
# ---------------------------------------------------------------------------

def contract_to_eval_index(
    contract: TaskContract,
    envmap: Mapping[str, Any] | Any,
) -> EvalIndex:
    """Build an EvalIndex of authoritative checks from a TaskContract."""
    workspace_root: str = ""
    if hasattr(envmap, "workspace_root"):
        workspace_root = envmap.workspace_root

    checks: list[CheckSpec] = []
    seen_cmds: set[str] = set()

    def _add(command: str, label: str) -> None:
        cmd = command.strip()
        if not cmd or cmd in seen_cmds:
            return
        seen_cmds.add(cmd)
        checks.append(
            CheckSpec(
                check_id=_check_id("contract", cmd),
                label=label,
                command=cmd,
                origin="contract",
                authoritative=True,
            )
        )

    # 1. File-existence checks per deliverable.
    for cd in contract.deliverables:
        path = normalize_relpath(cd.path, workspace_root)
        if not path or path.startswith("tests/"):
            continue
        _add(f"test -e {path}", f"exists:{path}")

    # 2. Schema-key checks per output schema.
    for schema in contract.output_schemas:
        target = normalize_relpath(schema.target, workspace_root)
        if not target or not schema.required_keys:
            continue
        cmd = _schema_check_command(target, schema.required_keys)
        _add(cmd, f"schema:{target}")

    # Do not compile contract.required_checks. Those are model-authored
    # checklist prose and may contain placeholders or vague evaluation text.
    # Only harness-constructed existence, schema, and threshold checks above
    # are authoritative.

    # 3. File-size threshold checks.
    for ct in contract.thresholds:
        if not ct.source.startswith("file_size_bytes:"):
            continue
        file_path = ct.source.split(":", 1)[1].strip()
        norm_path = normalize_relpath(file_path, workspace_root)
        if not norm_path:
            continue
        # Convert target based on unit.
        if ct.unit.upper() in ("MB", "M"):
            byte_target = _mb_to_bytes(ct.target)
        elif ct.unit.upper() in ("KB", "K"):
            byte_target = int(ct.target * 1024)
        elif ct.unit.upper() in ("GB", "G"):
            byte_target = int(ct.target * 1024 * 1024 * 1024)
        else:
            byte_target = int(ct.target)
        test_op = _comparator_to_test_op(ct.comparator)
        cmd = f"test $(stat -c%s {norm_path}) {test_op} {byte_target}"
        _add(cmd, f"size:{norm_path}")

    return EvalIndex(checks=tuple(checks))


# ---------------------------------------------------------------------------
# RuntimeConfigIR from contract
# ---------------------------------------------------------------------------

def contract_to_runtime_ir(
    contract: TaskContract,
    compiler: ConfigCompiler,
    envmap: EnvMap,
) -> tuple[RuntimeConfigIR, tuple[str, ...]]:
    """Build a RuntimeConfigIR from a TaskContract, then repair it.

    Returns ``(ir, repair_codes)``.
    """
    from aether_next.repair import repair_config

    workspace_root = envmap.workspace_root

    # Filter capabilities to those available in the registry.
    available = set(compiler.registry.available_ids())
    selected = tuple(c for c in contract.capabilities if c in available)
    if not selected:
        selected = tuple(
            c for c in ("shell", "filesystem") if c in available
        ) or ("shell", "filesystem")

    # Workflow
    mode = contract.workflow if contract.workflow in WORKFLOW_MODES else "direct_build"

    # Inspection plan: parent dirs of deliverables + visible files.
    inspection_items: list[str] = []
    seen_insp: set[str] = set()
    for cd in contract.deliverables:
        parent = os.path.dirname(normalize_relpath(cd.path, workspace_root))
        if parent and parent not in seen_insp:
            seen_insp.add(parent)
            inspection_items.append(parent)
    if hasattr(envmap, "visible_files"):
        for vf in envmap.visible_files[:10]:
            norm = normalize_relpath(vf, workspace_root)
            if norm and norm not in seen_insp:
                seen_insp.add(norm)
                inspection_items.append(norm)

    # Proof plan from success_definition + stop_conditions.
    proof_items: list[str] = []
    if contract.success_definition:
        proof_items.append(contract.success_definition)
    proof_items.extend(contract.stop_conditions)

    # Check plan: IDs of the generated eval checks.
    eval_index = contract_to_eval_index(contract, envmap)
    check_ids = tuple(c.check_id for c in eval_index.checks)

    # Forbidden paths = things_to_preserve.
    forbidden = tuple(
        normalize_relpath(p, workspace_root)
        for p in contract.things_to_preserve
        if normalize_relpath(p, workspace_root)
    )

    # Build a task-specific solver identity from the contract.
    identity_parts = ["You are a careful, methodical software engineer."]
    if contract.success_definition:
        identity_parts.append(f"Success: {contract.success_definition}")
    if contract.constraints:
        identity_parts.append("Constraints: " + "; ".join(contract.constraints[:5]))

    # Carry failure_hypotheses, tooling_notes, and stop_conditions as advisory.
    advisory: list[str] = []
    if contract.failure_hypotheses:
        advisory.append("Likely failures: " + ", ".join(contract.failure_hypotheses))
    if contract.tooling_notes:
        advisory.append("Tooling: " + ", ".join(contract.tooling_notes))
    if contract.stop_conditions:
        advisory.append("Submit when: " + "; ".join(contract.stop_conditions))

    ir = RuntimeConfigIR(
        architect_summary=contract.task_understanding[:200],
        solver_identity_prompt=" ".join(identity_parts),
        selected_capabilities=selected,
        process_policy=ProcessPolicy(mode="stateless_shell"),
        workflow_policy=WorkflowPolicy(mode=mode),
        inspection_plan=tuple(inspection_items),
        proof_plan=tuple(proof_items),
        check_plan=check_ids,
        forbidden_paths=forbidden,
        advisory_notes=tuple(advisory),
        success_definition=contract.success_definition,
    )

    objective_graph = contract_to_objective_graph(contract, envmap)
    repaired_ir, repair_codes = repair_config(
        ir, compiler, envmap,
        objective_graph=objective_graph,
        eval_index=eval_index,
    )
    return repaired_ir, repair_codes
