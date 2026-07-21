"""Tests for compaction-preserve continuity in receipt-driven variant.

Three sentinels:
1. Receipt-driven rebase preserves continuity (durable facts survive).
2. Baseline/off rebase is byte-unchanged (flag-off identity).
3. Known-bad / falsifiable: dropped fact is restored post-rebase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.aether2.runtime.compactor import (
    build_receipt_continuity_snapshot,
    rebase,
)
from harness.aether2.runtime.context import ContextManager
from harness.aether2.runtime.run_config import ContextPackPolicy, validate_context_pack_policy
from harness.aether2.traces.receipt_store import QueryableReceiptStore
from harness.aether2.traces.task_local_tools import TaskLocalToolRegistry


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
    def call(
        self,
        messages: list[dict[str, Any]],
        tools: list,
        *,
        cache_prefix_len: int = 0,
    ) -> str:
        return "Handoff summary after rebase."


def _build_context(delta: StubDeltaState, generation: int = 0) -> ContextManager:
    ctx = ContextManager(delta_state=delta, compaction_generation=generation)
    ctx.build_prefix(
        system_prompt="You are a test agent.",
        task_instruction="Complete the task.",
        orientation={"goal": "test"},
        tool_schemas=[{"name": "bash", "description": "run commands"}],
    )
    ctx.append_turn(
        {"role": "user", "content": "step 1"},
        {"role": "assistant", "content": "done step 1"},
    )
    return ctx


def _make_receipt_store(
    tmp_path: Path,
    *,
    plan_text: str = "- step A\n- step B",
    contract: dict[str, Any] | None = None,
    add_candidate: bool = True,
    add_failure: bool = True,
    add_verifier_feedback: bool = True,
) -> QueryableReceiptStore:
    store = QueryableReceiptStore(root=tmp_path, run_id="test_run")
    store.set_success_contract(contract or {"source": "test", "contract_text": "must pass checks"})
    store.update_plan(step=1, plan_text=plan_text, reason="initial")
    if add_candidate:
        store.append("candidate_event", 1, "candidate started", {
            "candidate_event_type": "candidate_started",
            "candidate": {"candidate_id": "svc_1", "ports": ["8080"], "locked": True},
        })
    if add_failure:
        store.append("tool_result", 2, "bash failed: exit 1", {
            "tool_name": "bash",
            "exit_code": 1,
            "stderr_excerpt": "No such file",
        })
    if add_verifier_feedback:
        store.record_verification_feedback(
            step=3, ready=False, feedback={"issue": "tests not passing"}
        )
    return store


# ---- Test 1: Receipt-driven rebase preserves continuity ----

def test_receipt_driven_rebase_preserves_continuity(tmp_path: Path) -> None:
    """After rebase with receipt_continuity_snapshot, durable facts survive."""
    delta = StubDeltaState()
    ctx = _build_context(delta)
    model = StubModelClient()
    store = _make_receipt_store(tmp_path)
    local_tools = TaskLocalToolRegistry(root=tmp_path)
    local_tools.observe_tool_invocation(
        step=4,
        tool_name="write_file",
        arguments={"path": ".aether/tools/helper.py"},
        exit_code=0,
        evidence_id="evt_00001",
        files_changed=[".aether/tools/helper.py"],
    )
    policy = ContextPackPolicy()
    proof_state = {
        "state": "weak",
        "summary": "Receipt-backed evidence exists, but it is still thin.",
        "score": 1,
        "delta": 1,
        "rejected_proxy_evidence": [
            ".aether/tools/helper.py: task-local helper is not trusted for completion",
        ],
    }

    snapshot = build_receipt_continuity_snapshot(
        store,
        policy,
        local_tools=local_tools.summary(),
        proof_state=proof_state,
    )
    assert snapshot is not None

    rebased = rebase(ctx, model, receipt_continuity_snapshot=snapshot)

    # Find the [receipt_continuity] message in rebased prefix
    continuity_msgs = [
        m for m in rebased.prefix.messages
        if isinstance(m.get("content", ""), str)
        and "[receipt_continuity]" in m["content"]
    ]
    assert len(continuity_msgs) == 1, "receipt_continuity message must appear once"

    content = continuity_msgs[0]["content"]
    payload = json.loads(content.split("[receipt_continuity]\n", 1)[1])

    # Verify each durable fact category is present
    assert "success_contract" in payload, "success_contract must survive rebase"
    assert payload["success_contract"]["contract_text"] == "must pass checks"

    assert "plan" in payload, "plan must survive rebase"
    assert payload["plan"]["version"] >= 1

    assert "active_candidates" in payload, "active_candidates must survive rebase"
    assert len(payload["active_candidates"]) >= 1

    assert "recent_failures" in payload, "recent_failures must survive rebase"
    assert len(payload["recent_failures"]) >= 1

    assert "verifier_feedback" in payload, "verifier_feedback must survive rebase"
    assert len(payload["verifier_feedback"]) >= 1

    assert "local_tools" in payload, "task-local tools must survive rebase"
    assert payload["proof_state"]["state"] == "weak"
    assert payload["proof_state_delta"] == 1
    assert payload["rejected_proxy_evidence"] == [
        ".aether/tools/helper.py: task-local helper is not trusted for completion",
    ]


# ---- Test 2: Baseline/off rebase is byte-unchanged ----

def test_baseline_rebase_byte_identity(tmp_path: Path) -> None:
    """Without receipt-driven variant (snapshot=None), rebase output is identical."""
    delta = StubDeltaState()
    model = StubModelClient()

    ctx_a = _build_context(delta)
    ctx_b = _build_context(delta)

    rebased_baseline = rebase(ctx_a, model)
    rebased_none = rebase(ctx_b, model, receipt_continuity_snapshot=None)

    assert rebased_baseline.prefix.frozen_bytes == rebased_none.prefix.frozen_bytes, (
        "baseline rebase must be byte-identical with and without explicit None snapshot"
    )

    # Also verify no [receipt_continuity] message exists
    for msg in rebased_baseline.prefix.messages:
        content = msg.get("content", "")
        assert "[receipt_continuity]" not in content, (
            "baseline rebase must not contain receipt_continuity"
        )


# ---- Test 3: Known-bad / falsifiable: dropped fact is restored ----

def test_dropped_fact_restored_after_rebase(tmp_path: Path) -> None:
    """A durable fact present in the receipt store but absent from pre-rebase
    context is restored into the post-rebase context. This test FAILS without
    the compaction-preserve change (i.e., when receipt_continuity_snapshot is
    not wired through rebase)."""
    delta = StubDeltaState()
    ctx = _build_context(delta)
    model = StubModelClient()

    # The receipt store has a plan and contract, but the pre-rebase context
    # has NO receipt_context message (simulating the fact being dropped by
    # transcript truncation before compaction fires).
    store = _make_receipt_store(tmp_path)
    policy = ContextPackPolicy()

    # Confirm the plan is NOT in the pre-rebase context
    pre_rebase_text = json.dumps([m.get("content", "") for m in ctx.message_history()])
    assert "step A" not in pre_rebase_text, "precondition: plan must not be in pre-rebase context"

    # Rebase WITH the continuity snapshot
    snapshot = build_receipt_continuity_snapshot(store, policy)
    rebased = rebase(ctx, model, receipt_continuity_snapshot=snapshot)

    # The plan must now be present in the rebased context
    rebased_text = json.dumps([m.get("content", "") for m in rebased.prefix.messages])
    assert "step A" in rebased_text, (
        "KNOWN-BAD: plan from receipt store was not restored into rebased context"
    )

    # Counter-test: rebase WITHOUT the snapshot — plan must NOT appear
    ctx2 = _build_context(delta)
    rebased_without = rebase(ctx2, model, receipt_continuity_snapshot=None)
    rebased_without_text = json.dumps([m.get("content", "") for m in rebased_without.prefix.messages])
    assert "step A" not in rebased_without_text, (
        "Without receipt continuity, the plan should NOT appear in rebased context"
    )


def test_architect_policy_cannot_drop_recent_tool_and_verifier_outputs(tmp_path: Path) -> None:
    store = QueryableReceiptStore(root=tmp_path, run_id="test_run")
    store.record_tool_result(
        step=1,
        tool_name="run_command",
        arguments={"cmd": "python3 -m pytest"},
        exit_code=0,
        stdout="pytest passed with 3 tests",
        stderr="",
        raw_log_path="/tmp/current_run/run_command.log",
        files_changed=[],
    )
    store.record_tool_result(
        step=2,
        tool_name="read_file",
        arguments={"path": "README.md"},
        exit_code=0,
        stdout="important instruction",
        stderr="",
        raw_log_path=None,
        files_changed=[],
    )
    store.record_tool_result(
        step=3,
        tool_name="write_file",
        arguments={"path": "answer.txt"},
        exit_code=0,
        stdout="wrote answer.txt",
        stderr="",
        raw_log_path=None,
        files_changed=["answer.txt"],
    )
    store.record_tool_result(
        step=4,
        tool_name="run_command",
        arguments={"cmd": "python3 missing.py"},
        exit_code=2,
        stdout="",
        stderr="No such file",
        raw_log_path="/tmp/current_run/failure.log",
        files_changed=[],
    )
    store.record_artifact_observation(
        step=5,
        path="answer.txt",
        mode="read",
        status="present",
        summary="answer artifact observed",
    )
    store.record_verification_feedback(
        step=6,
        ready=False,
        feedback={"issue": "expected output not yet proven"},
    )
    policy = validate_context_pack_policy(
        {
            "include_sections": ["success_contract"],
            "always_include": ["success_contract"],
            "exclude_sections": [
                "recent_steps",
                "recent_failures",
                "verifier_feedback",
                "artifact_observations",
                "evidence_refs",
            ],
            "receipt_event_budget": 0,
            "failure_event_budget": 0,
            "tool_result_budget": 0,
            "verifier_feedback_budget": 0,
            "artifact_observation_budget": 0,
        }
    )

    snapshot = build_receipt_continuity_snapshot(store, policy)

    assert snapshot is not None
    rendered = json.dumps(snapshot, sort_keys=True)
    assert "run_command" in rendered
    assert "read_file" in rendered
    assert "write_file" in rendered
    assert "No such file" in rendered
    assert "expected output not yet proven" in rendered
    assert "answer artifact observed" in rendered
    assert "evidence_refs" in snapshot
    assert {"evt_00001", "evt_00004"}.issubset(
        {str(ref.get("event_id")) for ref in snapshot["evidence_refs"]}
    )
    assert all(ref.get("raw_log_available") is True for ref in snapshot["evidence_refs"])
    assert "run_command.log" not in rendered
    assert "failure.log" not in rendered
