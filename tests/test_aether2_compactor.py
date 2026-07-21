"""Mechanism sentinel: compaction fact preservation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from harness.aether2.runtime.compactor import build_fact_ledger, rebase, should_rebase
from harness.aether2.runtime.context import ContextManager


CRITICAL_REQUIREMENT = "CRITICAL_REQUIREMENT: use_python3_not_python2"


@dataclass
class StubDeltaState:
    files: dict[str, str] = field(default_factory=dict)
    artifact_registry: dict[str, Any] = field(default_factory=dict)
    workspace_root: str = "/workspace/test"
    job_registry: dict[str, Any] = field(default_factory=dict)
    session_registry: dict[str, Any] = field(default_factory=dict)
    service_registry: dict[str, Any] = field(default_factory=dict)
    process_registry: dict[str, Any] = field(default_factory=dict)
    installed_packages: list[Any] = field(default_factory=list)
    nonzero_exits: list[Any] = field(default_factory=list)
    evidence_ledger: dict[str, Any] = field(default_factory=dict)


class StubModelClient:
    """Returns a deterministic handoff summary preserving the fact ledger."""

    def call(self, messages: list[dict[str, Any]], tools: list, *, cache_prefix_len: int = 0) -> str:
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and "fact_ledger" in content:
                return f"Handoff summary. Preserved fact: {CRITICAL_REQUIREMENT}"
        return "Handoff summary with no facts."


def _build_context(delta_state: StubDeltaState, generation: int = 0) -> ContextManager:
    ctx = ContextManager(delta_state=delta_state, compaction_generation=generation)
    ctx.build_prefix(
        system_prompt="You are a test agent.",
        task_instruction=f"Complete the task. {CRITICAL_REQUIREMENT}",
        orientation={"goal": "test", "requirement": CRITICAL_REQUIREMENT},
        tool_schemas=[{"name": "bash", "description": "run commands"}],
    )
    return ctx


def test_should_rebase_threshold() -> None:
    assert should_rebase(0.59, False) is False
    assert should_rebase(0.60, False) is True
    assert should_rebase(0.30, True) is True


def test_build_fact_ledger_preserves_workspace_root() -> None:
    delta = StubDeltaState(
        files={"main.py": "abc123"},
        workspace_root="/workspace/test",
    )
    ledger = build_fact_ledger(
        delta,
        orientation={"requirement": CRITICAL_REQUIREMENT},
    )
    assert ledger["workspace_root"] == "/workspace/test"
    assert ledger["orientation"]["requirement"] == CRITICAL_REQUIREMENT
    assert len(ledger["written_files"]) == 1


def test_compaction_fact_preservation() -> None:
    """Plant a decisive fact early and verify it survives N rebase cycles."""
    delta = StubDeltaState(
        files={"main.py": "abc123", "config.json": "def456"},
        workspace_root="/workspace/test",
    )
    model_client = StubModelClient()
    ctx = _build_context(delta, generation=0)

    # Plant the critical requirement in transcript turns
    ctx.append_turn(
        {"role": "user", "content": f"Remember: {CRITICAL_REQUIREMENT}"},
        {"role": "assistant", "content": f"Acknowledged: {CRITICAL_REQUIREMENT}"},
    )

    num_cycles = 5
    for cycle in range(num_cycles):
        # Add some filler turns before each rebase
        ctx.append_turn(
            {"role": "user", "content": f"Step {cycle}: do something"},
            {"role": "assistant", "content": f"Done step {cycle}"},
        )

        rebased = rebase(ctx, model_client)

        # Verify generation increments
        assert rebased.compaction_generation == ctx.compaction_generation + 1

        # Verify the task instruction (which contains the critical fact) survives
        assert CRITICAL_REQUIREMENT in rebased.task_instruction

        # Verify orientation preserved
        assert rebased.orientation.get("requirement") == CRITICAL_REQUIREMENT

        # Verify the fact ledger embedded in the prefix contains workspace_root
        prefix_messages = rebased.prefix.messages
        fact_ledger_msg = [
            m for m in prefix_messages
            if isinstance(m.get("content", ""), str)
            and "[deterministic_fact_ledger]" in m["content"]
        ]
        assert len(fact_ledger_msg) == 1, f"cycle {cycle}: missing fact ledger in prefix"
        ledger_content = fact_ledger_msg[0]["content"]
        ledger_json_str = ledger_content.split("[deterministic_fact_ledger]\n", 1)[1]
        ledger_data = json.loads(ledger_json_str)
        assert ledger_data["workspace_root"] == "/workspace/test", (
            f"cycle {cycle}: workspace_root lost"
        )
        assert ledger_data["orientation"]["requirement"] == CRITICAL_REQUIREMENT, (
            f"cycle {cycle}: critical requirement lost from orientation"
        )

        # Verify delta_state is shared (not copied)
        assert rebased.delta_state is delta

        ctx = rebased

    # After all cycles, verify final state
    assert ctx.compaction_generation == num_cycles
    assert CRITICAL_REQUIREMENT in ctx.task_instruction


def test_compaction_preserves_explicit_frozen_success_contract_only() -> None:
    delta = StubDeltaState(workspace_root="/workspace/test")
    model_client = StubModelClient()
    contract_text = "Keep `python3` spelled exactly."

    ctx = ContextManager(delta_state=delta)
    ctx.build_prefix(
        system_prompt="You are a test agent.",
        task_instruction=f"Complete the task. {CRITICAL_REQUIREMENT}",
        orientation={"goal": "test"},
        tool_schemas=[],
        frozen_success_contract={
            "source": "task_pack",
            "contract_text": contract_text,
            "verbatim_lines": [contract_text],
        },
    )

    rebased = rebase(ctx, model_client)

    assert rebased.current_frozen_success_contract()["source"] == "task_pack"
    assert rebased.current_frozen_success_contract_text() == contract_text
    assert any(
        message["content"] == "[frozen_success_contract]\n" + contract_text
        for message in rebased.prefix.messages
    )
