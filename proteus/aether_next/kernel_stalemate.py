"""Verifier-disagreement stalemate check, extracted from kernel.py for the
500-LOC cap.

Pure move of ``AetherNextKernel.run``'s post-verdict stalemate-window body
(the ``if verdict is not None and verdict.verdict != "completed":`` block,
covering both the ``verifier_stalemate`` and ``verifier_blocked_stalemate``
outcomes) into a module-level function. ``KernelResult`` is defined in
kernel.py, which imports this module at load time, so it is imported here
only under ``TYPE_CHECKING`` (for the annotation) and via a deferred
in-function import (for actual construction) to avoid a cycle -- the same
pattern verify_inspection_requests.py already uses for ``_model_output_error``.
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .ledger import ExecutionLedger, Receipt
from .runtime_ir import CompiledRuntime

if TYPE_CHECKING:
    from .kernel import KernelResult


def check_verifier_stalemate(
    kernel: Any,
    verdict: Any,
    step: int,
    reconfigurations: int,
    compiled: CompiledRuntime,
    ledger: ExecutionLedger,
    architect_defect_reasons: list[str],
    verifier_round_finding_sets: list[frozenset[str]],
) -> "KernelResult | None":
    """Bounded verifier-disagreement check for a non-completed verdict.

    Caller must only call this when ``verdict is not None and verdict.verdict
    != "completed"`` (the original inline guard stays in kernel.py). Mutates
    ``verifier_round_finding_sets`` in place -- same list object the caller
    holds -- exactly as the inline code did. Returns the terminal
    ``KernelResult`` when the identical finding set (or a blocked/uncertain
    verdict with no findings) has survived ``kernel.STALEMATE_ROUNDS``
    consecutive rounds; returns ``None`` when no stalemate is reached yet, in
    which case the caller continues its own post-verdict handling.
    """
    from .kernel import KernelResult

    active_ids = frozenset(
        str(item.get("finding_id", ""))
        for item in ledger.active_finding_context(step + 1)
        if str(item.get("finding_id", "")).strip()
    )
    verifier_round_finding_sets.append(active_ids)
    window = verifier_round_finding_sets[-kernel.STALEMATE_ROUNDS:]
    if not (
        len(window) == kernel.STALEMATE_ROUNDS
        and all(entry == window[0] for entry in window)
    ):
        return None
    if window[0]:
        ledger.record(Receipt(
            receipt_id=f"step-{step}:verifier_stalemate",
            step=step,
            kind="verifier_stalemate",
            success=False,
            summary=(
                f"verifier stalemate: the same {len(window[0])} finding(s) "
                f"survived {kernel.STALEMATE_ROUNDS} verification rounds with "
                "intervening solver evidence; harness records the disagreement "
                "and terminates without picking a winner"
            ),
            failure_class="verifier_stalemate",
            payload={
                "rounds": kernel.STALEMATE_ROUNDS,
                "finding_ids": sorted(window[0]),
                "round_history": [sorted(entry) for entry in verifier_round_finding_sets],
                "final_verifier_verdict": verdict.as_dict(),
                "active_findings": ledger.active_finding_context(step + 1),
            },
        ))
        return KernelResult(
            status="verifier_stalemate", step=step,
            reconfigurations=reconfigurations,
            blockers=tuple(sorted(window[0])),
            env_digest=compiled.env_digest,
            receipts=ledger.all_receipts(),
            architect_defect=bool(architect_defect_reasons),
            architect_defect_reasons=tuple(architect_defect_reasons),
        )
    ledger.record(Receipt(
        receipt_id=f"step-{step}:verifier_blocked_stalemate",
        step=step,
        kind="verifier_blocked_stalemate",
        success=False,
        summary=(
            f"verifier blocked/uncertain without actionable findings for "
            f"{kernel.STALEMATE_ROUNDS} repeated verification rounds; "
            "harness records the disagreement and terminates without picking a winner"
        ),
        failure_class="verifier_blocked_stalemate",
        payload={
            "rounds": kernel.STALEMATE_ROUNDS,
            "round_history": [sorted(entry) for entry in verifier_round_finding_sets],
            "final_verifier_verdict": verdict.as_dict(),
            "active_findings": ledger.active_finding_context(step + 1),
        },
    ))
    return KernelResult(
        status="verifier_blocked_stalemate", step=step,
        reconfigurations=reconfigurations,
        blockers=(),
        env_digest=compiled.env_digest,
        receipts=ledger.all_receipts(),
        architect_defect=bool(architect_defect_reasons),
        architect_defect_reasons=tuple(architect_defect_reasons),
    )
