"""Timed-out Verifier generations lose all active-run authority."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from aether_next.kernel_verifier import _call_verify_with_timeout
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.verifier_generation import GenerationBoundLedger, VerifierGeneration


def test_generation_bound_ledger_quarantines_late_mutation() -> None:
    ledger = ExecutionLedger()
    generation = VerifierGeneration()
    guarded = GenerationBoundLedger(ledger, generation)
    generation.expire("test timeout")

    guarded.record(Receipt(
        receipt_id="late",
        step=1,
        kind="late_verifier_receipt",
        success=True,
        summary="must not enter active ledger",
    ))
    guarded.record_accounting(
        receipt_id="late-accounting",
        step=1,
        counter="model_verifier_calls",
        event="late",
    )

    assert ledger.all_receipts() == ()
    assert len(generation.quarantined_snapshot()) == 2


def test_timed_out_plain_verifier_cannot_record_late_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AETHER_MODEL_VERIFIER_TIMEOUT_S", "0.02")
    ledger = ExecutionLedger()

    def verify(packet: dict[str, Any], compiled: Any, guarded_ledger: Any) -> dict[str, Any]:
        del packet, compiled
        time.sleep(0.08)
        guarded_ledger.record(Receipt(
            receipt_id="late-verdict-side-effect",
            step=3,
            kind="model_verifier_result",
            success=True,
            summary="late completion",
        ))
        return {"verdict": "completed"}

    hooks = SimpleNamespace()
    with pytest.raises(TimeoutError, match="authority revoked"):
        _call_verify_with_timeout(
            hooks,
            verify,
            {"packet": "state"},
            SimpleNamespace(),
            ledger,
            step=3,
        )
    time.sleep(0.12)

    kinds = [receipt.kind for receipt in ledger.all_receipts()]
    assert kinds == ["verifier_generation_expired"]
    receipt = ledger.all_receipts()[0]
    assert receipt.payload["authority_revoked"] is True
    assert receipt.payload["late_ledger_mutations_allowed"] is False


def test_timed_out_verifier_cannot_start_late_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AETHER_MODEL_VERIFIER_TIMEOUT_S", "0.02")
    ledger = ExecutionLedger()
    calls: list[str] = []

    class Executor:
        def read_file(self, path: str) -> str:
            calls.append(path)
            return "secret"

    def verify_with_inspector(
        packet: dict[str, Any],
        compiled: Any,
        guarded_ledger: Any,
        inspector: Any,
    ) -> dict[str, Any]:
        del packet, compiled, guarded_ledger
        time.sleep(0.08)
        inspector((SimpleNamespace(
            request_id="late-read",
            kind="read_file",
            path="out.txt",
            handle="",
            check_id="",
            receipt_kind="",
            limit=1,
            command="",
            content="",
            target="",
            offset=0,
            span=0,
        ),))
        return {"verdict": "completed"}

    hooks = SimpleNamespace(verify_with_inspector=verify_with_inspector)
    with pytest.raises(TimeoutError):
        _call_verify_with_timeout(
            hooks,
            lambda *_args: None,
            {"packet": "state"},
            SimpleNamespace(),
            ledger,
            step=4,
            executor=Executor(),
            envmap=SimpleNamespace(workspace_root="/app"),
        )
    time.sleep(0.12)

    assert calls == []
    assert [item.kind for item in ledger.all_receipts()] == ["verifier_generation_expired"]


def test_successful_generation_may_record_before_result_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AETHER_MODEL_VERIFIER_TIMEOUT_S", "1")
    ledger = ExecutionLedger()

    def verify(packet: dict[str, Any], compiled: Any, guarded_ledger: Any) -> dict[str, Any]:
        del packet, compiled
        guarded_ledger.record(Receipt(
            receipt_id="on-time",
            step=1,
            kind="verifier_internal_evidence",
            success=True,
            summary="on-time evidence",
        ))
        return {"verdict": "needs_repair"}

    value = _call_verify_with_timeout(
        SimpleNamespace(),
        verify,
        {"packet": "state"},
        SimpleNamespace(),
        ledger,
        step=1,
    )
    assert value["verdict"] == "needs_repair"
    assert [item.receipt_id for item in ledger.all_receipts()] == ["on-time"]
