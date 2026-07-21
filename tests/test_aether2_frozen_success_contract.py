from __future__ import annotations

import json
from pathlib import Path

from harness.aether2.runtime.compactor import rebase
from harness.aether2.runtime.context import ContextManager
from harness.aether2.runtime.prompts import FROZEN_SUCCESS_CONTRACT_REMINDER
from harness.aether2.traces.receipts import ReceiptWriter


class _StubModelClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        self.calls.append(list(messages))
        return "handoff"


class _StubResponse:
    text = "ok"
    tool_calls: tuple[dict[str, object], ...] = ()


def _explicit_contract(task_instruction: str) -> dict[str, object]:
    return {
        "source": "task_pack",
        "contract_text": task_instruction,
        "verbatim_lines": task_instruction.splitlines(),
    }


def _build_context(task_instruction: str, *, frozen_success_contract: dict[str, object] | None = None) -> ContextManager:
    ctx = ContextManager()
    ctx.build_prefix(
        system_prompt="system",
        task_instruction=task_instruction,
        orientation={"cwd": "/workspace"},
        tool_schemas=[],
        frozen_success_contract=frozen_success_contract,
    )
    return ctx


def test_frozen_success_contract_is_omitted_by_default() -> None:
    task_instruction = "Write `out/final_submission.json`."
    ctx = _build_context(task_instruction)

    assert ctx.current_frozen_success_contract() == {}
    assert ctx.current_frozen_success_contract_text() == ""
    assert not any(
        message["content"].startswith("[frozen_success_contract]\n") for message in ctx.prefix.messages
    )

    rebased = rebase(ctx, _StubModelClient())

    assert rebased.current_frozen_success_contract() == {}
    assert rebased.current_frozen_success_contract_text() == ""
    assert not any(
        message["content"].startswith("[frozen_success_contract]\n") for message in rebased.prefix.messages
    )
    assert rebased.message_history()[1]["content"] == task_instruction
    ctx.assert_prefix_unchanged()
    rebased.assert_prefix_unchanged()


def test_explicit_frozen_success_contract_is_replayed_verbatim_across_rebase_cycles() -> None:
    task_instruction = "\n".join(
        [
            "Write `out/final_submission.json`.",
            "Use exact keys `python_command`, `workspace_root`, and `runner_command`.",
            "Run the exact command `python3 -m pytest tests/test_runner_contract.py -q`.",
            "Do not substitute nearby commands or proxy evidence.",
        ]
    )
    contract = _explicit_contract(task_instruction)
    ctx = _build_context(task_instruction, frozen_success_contract=contract)
    ctx.append_turn({"role": "assistant", "content": "working"})

    frozen_block = "[frozen_success_contract]\n" + task_instruction
    assert any(message["content"] == frozen_block for message in ctx.prefix.messages)

    rebased = rebase(ctx, _StubModelClient())

    assert rebased.current_frozen_success_contract_text() == task_instruction
    assert rebased.current_frozen_success_contract()["contract_text"] == task_instruction
    assert rebased.current_frozen_success_contract()["source"] == "task_pack"
    assert rebased.current_frozen_success_contract()["verbatim_lines"] == task_instruction.splitlines()
    assert any(message["content"] == frozen_block for message in rebased.prefix.messages)
    assert rebased.message_history()[1]["content"] == task_instruction
    ctx.assert_prefix_unchanged()
    rebased.assert_prefix_unchanged()


def test_frozen_success_contract_reminder_is_generic() -> None:
    assert "fsent_" not in FROZEN_SUCCESS_CONTRACT_REMINDER
    assert "TerminalBench" not in FROZEN_SUCCESS_CONTRACT_REMINDER
    assert "When a [frozen_success_contract] block is present" in FROZEN_SUCCESS_CONTRACT_REMINDER
    assert "do not compress or paraphrase" in FROZEN_SUCCESS_CONTRACT_REMINDER


def test_receipts_capture_explicit_frozen_success_contract_block(tmp_path: Path) -> None:
    task_instruction = "\n".join(
        [
            "Write `out/final_submission.json`.",
            "Use exact keys `python_command`, `workspace_root`, and `runner_command`.",
            "Run the exact command `python3 -m pytest tests/test_runner_contract.py -q`.",
        ]
    )
    ctx = _build_context(task_instruction, frozen_success_contract=_explicit_contract(task_instruction))
    writer = ReceiptWriter(tmp_path / "receipts")

    writer.record_model_exchange(
        1,
        ctx.message_history(),
        _StubResponse(),
        tool_schemas=[],
        call_role="normal",
        tail_state=ctx.current_tail_payload(),
        ledger_state={},
    )

    payload = json.loads((writer.receipts_dir / "model_exchange_1.json").read_text(encoding="utf-8"))
    frozen = payload["request_context"]["frozen_success_contract"]
    assert frozen["text"] == task_instruction
    assert frozen["digest"]
    assert "python_command" in frozen["text"]
    assert "runner_command" in frozen["text"]


def test_receipts_record_null_frozen_success_contract_when_absent(tmp_path: Path) -> None:
    ctx = _build_context("Write `out/final_submission.json`.")
    writer = ReceiptWriter(tmp_path / "receipts")

    writer.record_model_exchange(
        1,
        ctx.message_history(),
        _StubResponse(),
        tool_schemas=[],
        call_role="normal",
        tail_state=ctx.current_tail_payload(),
        ledger_state={},
    )

    payload = json.loads((writer.receipts_dir / "model_exchange_1.json").read_text(encoding="utf-8"))
    frozen = payload["request_context"]["frozen_success_contract"]
    assert frozen["text"] is None
    assert frozen["digest"] is None
