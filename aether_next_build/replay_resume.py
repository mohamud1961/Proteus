#!/usr/bin/env python3
"""Resumable replay: restore a workspace snapshot and run real solver steps.

Given a trace JSON and a workspace snapshot captured by ``--snapshot-dir``,
this script resumes execution from a checkpoint step, runs K additional
solver steps against the real filesystem, then evaluates planned checks.

Usage::

    python3.11 replay_resume.py \\
        --trace traces/task.trace.json \\
        --snapshot-dir snapshots/task/step_5 \\
        --resume-step 5 --steps 2 \\
        --solver-deploy-env AZURE_OPENAI_GPT54_MINI_DEPLOYMENT \\
        --out replay_result.json

No Docker required -- operates on a local copy of the snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any

_BUILD_DIR = str(Path(__file__).resolve().parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.compiler import CapabilityRegistry, ConfigCompiler  # noqa: E402
from aether_next.kernel import AetherNextKernel, KernelResult  # noqa: E402
from aether_next.ledger import Receipt  # noqa: E402
from aether_next.model_hooks import ModelHooks  # noqa: E402
from aether_next.real_executor import SubprocessExecutor  # noqa: E402
from aether_next.runtime_ir import (  # noqa: E402
    BootstrapPolicy,
    CapabilityDescriptor,
    CompletionPolicy,
    ContextPolicy,
    EnvMap,
    HelperToolPolicy,
    ProcessPolicy,
    ReconfigurePolicy,
    RefusalPolicy,
    RuntimeConfigIR,
    WorkflowPolicy,
)


# ---------------------------------------------------------------------------
# Trace loading helpers
# ---------------------------------------------------------------------------

def _load_trace(path: Path) -> dict[str, Any]:
    """Load a trace JSON file, tolerating both wrapped and bare formats."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("trace", data)  # type: ignore[return-value]


def _task_prompt_from_trace(trace: dict[str, Any]) -> str:
    """Extract the task prompt from the trace's prefix_messages."""
    for msg in trace.get("prefix_messages", []):
        content = msg.get("content", "")
        if content and len(content) > 20:
            return content
    return "Complete the task."


def _receipts_before_step(trace: dict[str, Any], step: int) -> list[Receipt]:
    """Reconstruct Receipt objects from trace steps before *step*."""
    receipts: list[Receipt] = []
    for s in trace.get("steps", []):
        if not isinstance(s, dict):
            continue
        if int(s.get("step", -1)) >= step:
            continue
        for obs in s.get("observations", []):
            receipts.append(Receipt(
                receipt_id=obs.get("receipt_id", "replay"),
                step=int(s["step"]),
                kind=obs.get("kind", "unknown"),
                success=bool(obs.get("success", False)),
                summary=obs.get("summary", ""),
                failure_class=obs.get("failure_class", ""),
            ))
    return receipts


# ---------------------------------------------------------------------------
# RuntimeConfigIR reconstruction from trace dict
# ---------------------------------------------------------------------------

_POLICY_CLASSES: dict[str, type] = {
    "context_policy": ContextPolicy,
    "process_policy": ProcessPolicy,
    "helper_tool_policy": HelperToolPolicy,
    "bootstrap_policy": BootstrapPolicy,
    "completion_policy": CompletionPolicy,
    "refusal_policy": RefusalPolicy,
    "reconfigure_policy": ReconfigurePolicy,
    "workflow_policy": WorkflowPolicy,
}


def _reconstruct_ir(config: dict[str, Any]) -> RuntimeConfigIR:
    """Rebuild a RuntimeConfigIR from a ``dataclasses.asdict`` dict."""
    kwargs: dict[str, Any] = {}
    ir_field_names = {f.name for f in dc_fields(RuntimeConfigIR)}
    for key, value in config.items():
        if key not in ir_field_names:
            continue
        if key in _POLICY_CLASSES and isinstance(value, dict):
            cls = _POLICY_CLASSES[key]
            valid_keys = {f.name for f in dc_fields(cls)}
            filtered = {k: v for k, v in value.items() if k in valid_keys}
            # Convert lists to tuples for frozen dataclasses.
            for fld in dc_fields(cls):
                if fld.name in filtered and "tuple" in str(fld.type):
                    filtered[fld.name] = tuple(filtered[fld.name])
            kwargs[key] = cls(**filtered)
        elif isinstance(value, list):
            kwargs[key] = tuple(value)
        else:
            kwargs[key] = value
    return RuntimeConfigIR(**kwargs)


# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------

def _run_checks(
    workspace: str,
    check_commands: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Execute check commands against the workspace and return results."""
    results: list[dict[str, Any]] = []
    for chk in check_commands:
        cmd = chk.get("command", "")
        check_id = chk.get("check_id", cmd[:40])
        if not cmd:
            continue
        try:
            proc = subprocess.run(
                ["bash", "-lc", cmd], cwd=workspace,
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            results.append({
                "check_id": check_id, "command": cmd,
                "passed": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout_tail": proc.stdout[-500:],
                "stderr_tail": proc.stderr[-500:],
            })
        except subprocess.TimeoutExpired:
            results.append({
                "check_id": check_id, "command": cmd,
                "passed": False, "exit_code": -1,
                "stdout_tail": "", "stderr_tail": "timeout",
            })
    return results


# ---------------------------------------------------------------------------
# Main replay logic
# ---------------------------------------------------------------------------

def replay_resume(
    *,
    trace_path: Path,
    snapshot_dir: str,
    resume_step: int,
    steps: int,
    solver_model: Any,
    architect_model: Any | None = None,
) -> dict[str, Any]:
    """Resume a run from a snapshot and trace checkpoint.

    Returns a JSON-serializable result dict.
    """
    trace = _load_trace(trace_path)
    config = trace.get("architect_config", {})
    if not config:
        return {"status": "error", "detail": "no architect_config in trace"}

    runtime_ir = _reconstruct_ir(config)
    task_prompt = _task_prompt_from_trace(trace)

    # Copy snapshot to a fresh tempdir as the workspace.
    work_dir = tempfile.mkdtemp(prefix="replay_resume_")
    try:
        # Copy snapshot contents into work_dir.
        if os.path.isdir(snapshot_dir):
            for item in os.listdir(snapshot_dir):
                src = os.path.join(snapshot_dir, item)
                dst = os.path.join(work_dir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)

        # Build envmap from the workspace + task prompt.
        from aether_next.envmap_builder import build_envmap_from_task
        envmap = build_envmap_from_task(work_dir, task_prompt, workspace_root=work_dir)

        # Compile the runtime.
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        obj_graph, eval_index = compiler.analyze_envmap(envmap)
        issues = compiler.validate(
            runtime_ir, envmap,
            objective_graph=obj_graph, eval_index=eval_index,
        )
        fatal = [i for i in issues if i.fatal]
        if fatal:
            return {
                "status": "error",
                "detail": "trace architect_config is not replayable",
                "config_invalid_blockers": [
                    {"code": issue.code, "message": issue.message}
                    for issue in fatal
                ],
            }
        compiled = compiler.compile(
            runtime_ir, envmap,
            objective_graph=obj_graph, eval_index=eval_index,
        )

        # Pre-seed ledger with receipts from prior steps.
        from aether_next.ledger import ExecutionLedger
        ledger = ExecutionLedger()
        ledger.seed_capabilities(compiled.selected_capability_ids())
        ledger.ensure_objective(compiled.objective_graph)
        prior_receipts = _receipts_before_step(trace, resume_step)
        for r in prior_receipts:
            ledger.record(r)

        # Build executor and hooks.
        executor = SubprocessExecutor(work_dir, default_timeout_s=120)
        if architect_model is None:
            architect_model = solver_model
        hooks = ModelHooks(architect_model, solver_model)

        # Run kernel for K more steps.
        kernel = AetherNextKernel(max_steps=resume_step + steps)
        result = kernel.run(envmap, executor, hooks)

        # Extract check commands from compiled runtime.
        check_commands = [
            {"check_id": c.check_id, "command": c.command}
            for c in compiled.planned_checks()
        ]
        check_results = _run_checks(work_dir, check_commands)

        return {
            "task": trace_path.stem.replace(".trace", ""),
            "resume_step": resume_step,
            "steps_run": steps,
            "final_step": result.step,
            "status": result.status,
            "check_results": check_results,
            "receipts": [
                {"receipt_id": r.receipt_id, "kind": r.kind,
                 "success": r.success, "summary": r.summary}
                for r in result.receipts
            ],
        }
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"{type(exc).__name__}: {exc}",
            "task": trace_path.stem.replace(".trace", ""),
            "resume_step": resume_step,
            "steps_run": 0,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Resume a run from a workspace snapshot + trace checkpoint.",
    )
    ap.add_argument("--trace", required=True, help="Path to trace JSON file.")
    ap.add_argument("--snapshot-dir", required=True,
                    help="Path to snapshot directory (e.g. snapshots/task/step_5).")
    ap.add_argument("--resume-step", type=int, required=True,
                    help="Step number to resume from.")
    ap.add_argument("--steps", type=int, default=2,
                    help="Number of additional steps to run (default: 2).")
    ap.add_argument("--solver-deploy-env",
                    default="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
                    help="Env var for solver deployment.")
    ap.add_argument("--solver-key-env",
                    default="AZURE_OPENAI_GPT54_MINI_KEY",
                    help="Env var for solver API key.")
    ap.add_argument("--endpoint-env",
                    default="AZURE_OPENAI_ENDPOINT",
                    help="Env var for Azure endpoint.")
    ap.add_argument("--effort", default="medium",
                    help="Reasoning effort (default: medium).")
    ap.add_argument("--out", default="replay_result.json",
                    help="Output JSON path (default: replay_result.json).")
    args = ap.parse_args(argv)

    from aether_next.providers.azure_model import make_azure_callable
    solver_model = make_azure_callable(
        deployment_env=args.solver_deploy_env,
        key_env=args.solver_key_env,
        endpoint_env=args.endpoint_env,
        effort=args.effort,
    )

    result = replay_resume(
        trace_path=Path(args.trace),
        snapshot_dir=args.snapshot_dir,
        resume_step=args.resume_step,
        steps=args.steps,
        solver_model=solver_model,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"Result written to {out}", flush=True)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
