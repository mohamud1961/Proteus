"""Run adapter: wire EnvMap + SubprocessExecutor + ModelHooks + Kernel into a single call."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .classifier import HarnessLimiterClassifier
from .envmap_builder import build_envmap_from_task
from .kernel import AetherNextKernel, KernelResult
from .model_hooks import ModelHooks, ModelCallable
from .real_executor import SubprocessExecutor
from .result_metrics import run_metrics_for_row
from .task_metadata_loader import load_task_metadata
from .task_contract import TaskClause, TaskContract
from .world import WorldState



def _load_task_toml(task_dir: str) -> dict[str, Any]:
    return load_task_metadata(task_dir)

def ensure_certified_architect_mode(architect_mode: str) -> None:
    """Fail closed: the certified harness supports only the workbench architect.

    Legacy ir/contract architect modes are physically quarantined in
    ``reference_legacy`` and cannot be reached from this adapter.
    """
    if architect_mode == "workbench":
        return
    raise ValueError(
        "legacy architect modes are quarantined in reference_legacy; "
        "the certified harness supports only architect_mode='workbench'"
    )


def _receipt_summary(result: KernelResult) -> list[dict[str, Any]]:
    """Compact receipt summaries for the run record."""
    items: list[dict[str, Any]] = []
    for receipt in result.receipts:
        items.append({
            "receipt_id": receipt.receipt_id,
            "kind": receipt.kind,
            "success": receipt.success,
            "failure_class": receipt.failure_class,
            "summary": receipt.summary,
        })
    return items


def workbench_architect_for(architect_model: ModelCallable, *, hooks: ModelHooks | None = None) -> Any:
    """Construct the certified workbench architect for a model callable.

    Supplying the owning hooks preserves provider telemetry attribution for the
    architect's initial and repair calls.  The raw-callable form remains for
    compatibility with offline callers and existing tests.
    """
    from .workbench_hooks import WorkbenchArchitect
    model = hooks.call_architect_model if hooks is not None else architect_model
    return WorkbenchArchitect(model)


def run_task(
    *,
    task_dir: str,
    instruction_text: str,
    architect_model: ModelCallable,
    solver_model: ModelCallable,
    verifier_model: ModelCallable | None = None,
    workspace_root: str = "/app",
    max_steps: int = 24,
    architect_mode: str = "workbench",
) -> dict[str, Any]:
    """Execute a task end-to-end and return a JSON-serializable run record.

    Parameters
    ----------
    task_dir:
        Path to the task directory for building the EnvMap.
    instruction_text:
        The task instruction / prompt text.
    architect_model:
        Model callable for the architect role.
    solver_model:
        Model callable for the solver role.
    workspace_root:
        Workspace root for the executor (default ``/app``).
    max_steps:
        Maximum kernel steps.

    Returns
    -------
    dict
        JSON-serializable run record.
    """
    task_toml = _load_task_toml(task_dir)
    envmap = build_envmap_from_task(
        task_dir,
        instruction_text,
        workspace_root=workspace_root,
        task_toml=task_toml,
    )

    ensure_certified_architect_mode(architect_mode)

    executor = SubprocessExecutor(workspace_root)
    hooks = ModelHooks(
        architect_model,
        solver_model,
        verifier_model=verifier_model,
        task_id=Path(task_dir).name,
    )
    kernel = AetherNextKernel(
        max_steps=max_steps,
        workbench_architect=workbench_architect_for(architect_model, hooks=hooks),
        certified_production=True,
    )
    # Keep a task-scoped WorldState owned by the adapter so the production
    # boundary, not only the kernel's direct-call fallback, carries state into
    # every Verifier activation and can emit a final compact snapshot.
    world_state = WorldState(
        task_contract=TaskContract.create(
            instruction_text,
            (TaskClause("task:prompt", instruction_text),),
        ),
        env_facts={
            "workspace_root": workspace_root,
            "network_scope": envmap.network_scope,
            "visible_file_count": len(envmap.visible_files),
            "visible_dir_count": len(envmap.visible_dirs),
        },
    )
    result = kernel.run(envmap, executor, hooks, world_state=world_state)

    classifier = HarnessLimiterClassifier()
    classification = classifier.classify(result)

    run_metrics = run_metrics_for_row(result, hooks.last_parse_errors)
    model_call_telemetry = hooks.drain_model_telemetry()
    quarantined_model_call_telemetry = hooks.drain_quarantined_model_telemetry()

    return {
        "architect_mode": architect_mode,
        "status": result.status,
        "step": result.step,
        "reconfigurations": result.reconfigurations,
        "architect_defect": result.architect_defect,
        "architect_defect_reasons": list(result.architect_defect_reasons),
        "used_check_ids": list(result.used_check_ids),
        "blockers": list(result.blockers),
        "classifier_label": classification.label,
        "classifier_confidence": classification.confidence,
        "classifier_evidence": list(classification.evidence),
        "classifier_detail": classification.detail,
        "model_parse_errors": run_metrics.pop("model_parse_errors"),
        "run_metrics": run_metrics,
        "model_call_telemetry": list(model_call_telemetry),
        "quarantined_late_model_telemetry": list(quarantined_model_call_telemetry),
        "world_state_snapshot": world_state.dynamic_snapshot(),
        "receipt_summary": _receipt_summary(result),
    }


# ---------------------------------------------------------------------------
# Stub model for offline CLI use
# ---------------------------------------------------------------------------

class _StubWorkbenchArchitectModel:
    """Returns a valid HarnessConfigIR JSON for offline workbench mode."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        return json.dumps({
            "schema_version": "harness_config.v1",
            "task_understanding": "stub workbench: configure a local file task",
            "success_definition": "required local artifacts exist and visible checks pass",
            "solver_system_prompt": {
                "role": "Careful local task solver",
                "workflow": ["inspect visible files", "make the requested change", "self-verify", "submit"],
                "self_verification": ["run configured checks before submitting"],
                "memory_use": ["query_memory only before repeating reads/checks or retrieving prior evidence"],
                "stop_conditions": ["submit only after visible evidence supports completion"],
            },
            "verifier_system_prompt": {
                "role": "Read-only current-state verifier for the local task",
                "success_criteria": ["required local artifacts exist and visible checks pass"],
                "required_evidence": ["current artifact state and visible check evidence support completion"],
                "false_positive_traps": ["artifact presence alone may not prove semantic correctness"],
                "verdict_guidance": ["completed requires current evidence; needs_repair names the missing or wrong state"],
                "feedback_guidance": ["give concrete repair feedback tied to observed state"],
            },
            "context_policy": {"mode": "retrieval_augmented", "always_include": ["recent_progress", "pending_checks"]},
            "model_verifier_policy": {"enabled": True},
            "failure_feedback_policy": {"persist_until": "resolved_or_superseded"},
            "local_verification_limits": ["offline stub workbench does not prove hidden grader behavior"],
        })


class _StubSolverModel:
    """Scripted solver: submit_outcome immediately."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        return json.dumps({
            "kind": "submit_outcome",
            "summary": "stub solver: submitting outcome immediately",
        })


class _StubVerifierModel:
    """Returns a minimal completed verifier verdict for offline workbench runs."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        return json.dumps({
            "verdict": "completed",
            "confidence": "high",
            "summary": "Offline stub verifier observed sufficient local completion evidence.",
            "completion_evidence": ["offline_stub_verifier"],
        })


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an Aether-Next task with stub models (offline).",
    )
    parser.add_argument(
        "--task-dir",
        required=True,
        help="Path to the task directory.",
    )
    parser.add_argument(
        "--instruction-file",
        required=True,
        help="Path to a text file containing the task instruction.",
    )
    parser.add_argument(
        "--workspace-root",
        default="/tmp/aether_workspace",
        help="Workspace root directory (default: /tmp/aether_workspace).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=24,
        help="Maximum kernel steps (default: 24).",
    )
    args = parser.parse_args()

    instruction_path = Path(args.instruction_file)
    if not instruction_path.is_file():
        print(f"error: instruction file not found: {args.instruction_file}", file=sys.stderr)
        sys.exit(1)
    instruction_text = instruction_path.read_text(encoding="utf-8")

    task_dir = str(Path(args.task_dir).resolve())
    if not Path(task_dir).is_dir():
        print(f"error: task directory not found: {args.task_dir}", file=sys.stderr)
        sys.exit(1)

    record = run_task(
        task_dir=task_dir,
        instruction_text=instruction_text,
        architect_model=_StubWorkbenchArchitectModel(),
        solver_model=_StubSolverModel(),
        verifier_model=_StubVerifierModel(),
        workspace_root=args.workspace_root,
        max_steps=args.max_steps,
    )

    json.dump(record, sys.stdout, indent=2, default=str)
    print()


if __name__ == "__main__":
    _main()
