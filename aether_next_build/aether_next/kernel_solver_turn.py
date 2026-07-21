"""Solver parse-error handling (same-step retry), extracted from kernel.py
for the 500-LOC cap.

Pure move of ``AetherNextKernel.run``'s ``except ModelOutputError`` branch
for the solver turn into a module-level function. ``hooks`` is the
``KernelHooks`` instance (typed ``Any`` here, mirroring kernel_reconfigure.py's
existing ``hooks: Any`` parameter, to avoid a cycle with kernel.py's
``KernelHooks`` Protocol).
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, TYPE_CHECKING

from .ledger import ExecutionLedger, Receipt
from .model_hooks import ModelOutputError
from .redaction import redact_text_with_events
from .runtime_ir import CompiledRuntime, SolverTurn

if TYPE_CHECKING:
    from .tracing import RunTrace


def handle_solver_parse_error(
    hooks: Any,
    exc: ModelOutputError,
    step: int,
    compiled: CompiledRuntime,
    messages: list[dict[str, str]],
    ledger: ExecutionLedger,
    context_packet: Mapping[str, Any] | None,
    trace: "RunTrace | None",
    before_count: int,
) -> SolverTurn | None:
    """Record the parse-error receipt, retry once in the same step, and on a
    second failure record the retry receipt + trace step.

    Returns the retried ``SolverTurn`` on success. Returns ``None`` when the
    retry also failed -- the parse-error-retry receipt and trace step have
    already been recorded by this call; the caller must fire the step
    snapshot, advance ``step``, and ``continue`` the loop (a ``continue``
    cannot cross a function boundary, so that piece stays inline in
    kernel.py).
    """
    raw_output = str(getattr(hooks, "last_raw_solver_output", "") or "")
    redacted_output, redaction_events = redact_text_with_events(raw_output)
    ledger.record(Receipt(
        receipt_id=f"step-{step}:solver_parse_error",
        step=step,
        kind="solver_parse_error",
        success=False,
        summary=f"solver output parse/validation error: {exc}",
        failure_class="solver_protocol_error",
        payload={
            "error": str(exc),
            "redacted_output": redacted_output[:20000],
            "raw_output_sha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
            "raw_output_bytes": len(raw_output.encode("utf-8")),
            "redaction_events": list(redaction_events),
            "raw_output_storage": "protected_provider_evidence_only",
            "retry_attempted": True,
        },
    ))
    ledger.record_accounting(
        receipt_id=f"step-{step}:solver_malformed_attempt:{ledger.accounting_value('solver_malformed_provider_attempts') + 1}",
        step=step,
        counter="solver_malformed_provider_attempts",
        event="primary_solver_call_malformed",
    )
    retry_messages = list(messages) + [{
        "role": "user",
        "content": (
            "Your previous turn could not be parsed or validated. "
            f"Error: {exc}. Emit exactly one valid solver turn JSON object using the allowed schema. "
            "Action kinds are nested inside an act turn; they are never top-level turn kinds. "
            "Example act: {\"kind\":\"act\",\"summary\":\"probe service\",\"actions\":[{\"action_id\":\"probe1\",\"kind\":\"probe_service\",\"capability_id\":\"service_probe\",\"arguments\":{\"target\":\"127.0.0.1:6665\"},\"intent\":\"confirm readiness\",\"expected_observation\":\"live or not_live\",\"if_fail_next\":\"inspect logs\"}]}. "
            "Example submit: {\"kind\":\"submit_outcome\",\"summary\":\"task is complete with cited evidence\"}. "
            "Do not request reconfiguration; report a blocker only through the report_blocker action if needed."
        ),
    }]
    try:
        ledger.record_accounting(
            receipt_id=f"step-{step}:solver_protocol_correction:{ledger.accounting_value('solver_protocol_correction_calls') + 1}",
            step=step,
            counter="solver_protocol_correction_calls",
            event="same_step_protocol_correction",
        )
        ledger.record_accounting(
            receipt_id=f"step-{step}:solver_provider_turn:{ledger.accounting_value('solver_provider_turns') + 1}",
            step=step,
            counter="solver_provider_turns",
            event="protocol_correction_solver_call",
        )
        return hooks.solve(retry_messages, compiled)
    except ModelOutputError as retry_exc:
        raw_retry = str(getattr(hooks, "last_raw_solver_output", "") or "")
        redacted_retry, retry_redaction_events = redact_text_with_events(raw_retry)
        ledger.record(Receipt(
            receipt_id=f"step-{step}:solver_parse_error_retry",
            step=step,
            kind="solver_parse_error",
            success=False,
            summary=f"solver retry still invalid: {retry_exc}",
            failure_class="solver_protocol_error",
            payload={
                "error": str(retry_exc),
                "redacted_output": redacted_retry[:20000],
                "raw_output_sha256": hashlib.sha256(raw_retry.encode("utf-8")).hexdigest(),
                "raw_output_bytes": len(raw_retry.encode("utf-8")),
                "redaction_events": list(retry_redaction_events),
                "raw_output_storage": "protected_provider_evidence_only",
                "retry_attempted": False,
            },
        ))
        ledger.record_accounting(
            receipt_id=f"step-{step}:solver_malformed_attempt:{ledger.accounting_value('solver_malformed_provider_attempts') + 1}",
            step=step,
            counter="solver_malformed_provider_attempts",
            event="protocol_correction_malformed",
        )
        turn = SolverTurn(kind="act", summary="solver parse error placeholder", actions=())
        if trace is not None:
            trace.add_step(step, context_packet, turn, ledger.all_receipts()[before_count:])
        return turn
