"""Verifier-triggered single-shot reconfiguration, extracted from kernel.py.

Only a verifier ``blocked_by_harness_config`` verdict reaches this path; it
re-invokes the real workbench architect with the verdict as evidence and is
always recorded as an architect defect.
"""
from __future__ import annotations

from typing import Any

from .compiler import ConfigCompiler
from .kernel_config import resolve_runtime
from .ledger import ExecutionLedger, Receipt
from .runtime_ir import CompiledRuntime, EnvMap


def verifier_triggered_reconfigure(
kernel: Any,
hooks: Any,
    compiler: ConfigCompiler,
    envmap: EnvMap,
    compiled: CompiledRuntime,
    ledger: ExecutionLedger,
    verdict: Any,
    *,
    current_step: int,
) -> tuple[CompiledRuntime, bool]:
    """Single-shot, evidence-backed reconfiguration through the workbench
    architect, triggered only by a verifier ``blocked_by_harness_config``
    verdict.  Always recorded as an architect defect."""
    reconfig_request = {
        "reason": "verifier_blocked_by_harness_config",
        "verifier_verdict": verdict.as_dict(),
        "failure_clusters": ledger.failure_clusters(),
        "open_obligations": [ob.as_dict() for ob in ledger.open_obligations()],
    }
    resolved = resolve_runtime(
        envmap, compiler, hooks,
        workbench_architect=kernel.workbench_architect,
        reconfigure_context=reconfig_request,
    )
    if resolved.compiled is None:
        ledger.record(Receipt(
            receipt_id=f"step-{current_step}:verifier_reconfigure:invalid",
            step=current_step,
            kind="reconfigure_validation",
            success=False,
            summary=(
                "verifier-triggered reconfiguration invalid: "
                + (", ".join(resolved.fallback_codes) or "unknown")
            ),
            failure_class="config_invalid",
            payload={
                "architect_defect": True,
                "blockers": list(resolved.config_invalid_blockers),
            },
        ))
        return compiled, False
    new_compiled = resolved.compiled
    ledger.seed_capabilities(new_compiled.selected_capability_ids())
    ledger.record(Receipt(
        receipt_id=f"step-{current_step}:verifier_reconfigure:ok",
        step=current_step,
        kind="verifier_triggered_reconfigure",
        success=True,
        summary="single-shot reconfiguration triggered by verifier blocked_by_harness_config",
        state_change=True,
        payload={
            "architect_defect": True,
            "reconfigure_cause": "verifier_blocked_by_harness_config",
            "verifier_verdict": verdict.as_dict(),
        },
    ))
    ledger.record_config_realization(
        dict(new_compiled.config_realization),
        receipt_id="verifier-reconfig:realization",
    )
    return new_compiled, True
