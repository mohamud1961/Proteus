from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import time

from harness.aether2.control.completion import (
    _build_operational_verification_feedback,
)
from harness.aether2.control.execution_context import ExecutionContext
from harness.aether2.control.verification_context import _ReadOnlyVerificationContext
from harness.aether2.control.verification_rounds import _run_verification_rounds
from harness.aether2.runtime.context import ContextManager
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.run_config import ContextPackPolicy
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.runtime.verify import (
    DiscrepancyReport,
    RequirementResult,
    _downgrade_nonblocking_process_gaps,
    _uncovered_constraint_results,
    verify_fresh_context,
)
from harness.aether2.runtime.verify_report import _inspection_payload
from harness.aether2.traces.delta import with_evidence_ledger
from harness.aether2.traces.receipt_store import QueryableReceiptStore
from harness.aether2.traces.task_local_tools import TaskLocalToolRegistry


def _report(requirement: str, evidence: str, *, reason_codes: tuple[str, ...] = ()) -> DiscrepancyReport:
    return DiscrepancyReport(
        requirements=(
            RequirementResult(
                requirement=requirement,
                verdict="unverifiable",
                evidence=evidence,
                unresolved=True,
            ),
        ),
        reason_codes=reason_codes,
        summary=evidence,
        raw_response="{}",
    )


def test_operational_feedback_guides_exact_artifact_repairs():
    feedback = _build_operational_verification_feedback(
        _report(
            "The output must match the spec byte-for-byte.",
            "A visible shape check passed, but exact canonical formatting and precision remain unverifiable.",
            reason_codes=("canonical_output_unverifiable",),
        )
    )

    assert feedback["status"] == "not_ready"
    assert feedback["next_action_type"] == "rederive_and_compare_exact_artifact"
    assert "precision" in feedback["exact_guidance"]
    assert "weak check" in feedback["exact_guidance"]
    assert "shape check" in feedback["do_not_repeat"]


def test_operational_feedback_guides_service_survival_repairs():
    feedback = _build_operational_verification_feedback(
        _report(
            "Service must survive and answer the required endpoint.",
            "Startup passed, but bounded survival and shutdown behavior are not directly verified.",
            reason_codes=("service_survival_unverified",),
        )
    )

    assert feedback["next_action_type"] == "prove_service_survival_and_semantics"
    assert "fresh client probe" in feedback["exact_guidance"]
    assert "open port" in feedback["do_not_repeat"]


def test_hidden_grader_authority_notes_do_not_block_internal_readiness():
    requirements = (
        RequirementResult(
            requirement="The output must match the spec byte-for-byte.",
            verdict="satisfied",
            evidence="The visible checker passed and the artifact was read back.",
            evidence_strength="strong",
            evidence_provenance=("independent",),
        ),
    )

    extras = _uncovered_constraint_results(
        [
            "Hidden grading enforces exact byte comparison against the canonical output.",
            "Official grader remains the authority.",
        ],
        requirements,
    )

    assert extras == ()


def test_inferred_requirements_do_not_create_readiness_gaps():
    requirements = (
        RequirementResult(
            requirement="The output must match the spec byte-for-byte.",
            verdict="satisfied",
            evidence="The visible checker passed and the artifact was read back.",
            evidence_strength="strong",
            evidence_provenance=("independent",),
        ),
    )

    extras = _uncovered_constraint_results(
        ["[inferred] The service probably should survive a restart window."],
        requirements,
    )

    assert extras == ()


def test_inferred_requirement_result_does_not_block_readiness():
    report = DiscrepancyReport(
        requirements=(
            RequirementResult(
                requirement="[inferred] The service may need to survive a restart window.",
                verdict="unverifiable",
                evidence="No restart probe was collected.",
                unresolved=True,
                blocks_readiness=False,
            ),
        ),
        reason_codes=(),
        summary="restart window remains unverified",
        raw_response="{}",
    )

    assert report.unresolved_requirements == ()
    assert report.has_unresolved_gaps is False


def test_unobservable_planning_gap_does_not_block_task_readiness():
    requirements = (
        RequirementResult(
            requirement="Form a short plan before acting, then verify before calling completion.",
            verdict="unverifiable",
            evidence="The payload does not expose an explicit pre-action plan artifact.",
            evidence_strength="weak",
            unresolved=True,
        ),
    )

    normalized = _downgrade_nonblocking_process_gaps(requirements, {"tool_calls": []})

    assert normalized[0].verdict == "satisfied"
    assert normalized[0].unresolved is False


def test_absence_only_hidden_access_gap_does_not_block_without_hidden_action():
    requirements = (
        RequirementResult(
            requirement="Do not access reviewer or hidden assets.",
            verdict="unverifiable",
            evidence="There is no direct evidence proving absence of hidden/reviewer asset access.",
            evidence_strength="weak",
            unresolved=True,
        ),
    )

    normalized = _downgrade_nonblocking_process_gaps(
        requirements,
        {"tool_calls": [{"tool": "read_file", "arguments": {"path": "spec/format_spec.md"}}]},
    )

    assert normalized[0].verdict == "satisfied"
    assert normalized[0].unresolved is False


def test_hidden_access_gap_stays_blocking_when_action_mentions_hidden_path():
    requirements = (
        RequirementResult(
            requirement="Do not access reviewer or hidden assets.",
            verdict="unverifiable",
            evidence="There is no direct evidence proving absence of hidden/reviewer asset access.",
            evidence_strength="weak",
            unresolved=True,
        ),
    )

    normalized = _downgrade_nonblocking_process_gaps(
        requirements,
        {"tool_calls": [{"tool": "read_file", "arguments": {"path": "reviewer_pack/hidden_truth.json"}}]},
    )

    assert normalized[0].unresolved is True


def test_active_blockers_are_verifier_evidence_not_verifier_suppression(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = ContainerExecutor(workspace_root=workspace)
    ctx = ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(tmp_path / "state", backend=executor.backend, container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(tmp_path / "state", backend=executor.backend),
        raw_log_dir=tmp_path / "raw",
    )
    ledger = {
        "version": 1,
        "requirements": [
            {
                "requirement": "The produced service must answer the health endpoint.",
                "status": "unproven",
                "evidence_strength": "none",
                "evidence_refs": [],
                "verifier_blockers": ["No fresh client probe observed."],
                "next_required_evidence": ["Run a fresh client probe."],
            }
        ],
        "blockers": [
            {
                "status": "active",
                "requirement": "The produced service must answer the health endpoint.",
                "insufficiency_reason": "No fresh client probe observed.",
                "reason_codes": ["missing_service_probe"],
                "required_next_evidence": ["Run a fresh client probe."],
            },
        ],
    }
    ctx.last_snapshot = with_evidence_ledger(ctx.last_snapshot, ledger)
    context = ContextManager(delta_state=ctx.last_snapshot)
    context.build_prefix(
        system_prompt="kernel",
        task_instruction="Do the task.",
        orientation={"cwd": str(workspace), "workspace_root": str(workspace)},
        tool_schemas=[],
    )
    captured: dict[str, object] = {}

    def _fake_verify(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["claim"] = args[3]
        captured["action_digest"] = args[5]
        return _report(
            "The produced service must answer the health endpoint.",
            "No fresh client probe observed.",
            reason_codes=("missing_service_probe",),
        )

    def _fake_monitor(**kwargs):  # noqa: ANN003
        return {"applies": False}, kwargs["start_snapshot"]

    monkeypatch.setattr("harness.aether2.control.verification_rounds.verify_fresh_context", _fake_verify)
    monkeypatch.setattr("harness.aether2.control.verification_rounds._monitor_persistent_runtime", _fake_monitor)

    state = {
        "verification_rounds": 0,
        "verification_round_limit": 1,
        "feedback_only": True,
        "model_calls": 0,
        "tokens_cached": 0,
        "tokens_fresh": 0,
        "total_cost": 0.0,
        "compaction_count": 0,
        "recoveries": 0,
        "no_delta_streaks": 0,
        "finalize_pass": False,
        "finalize_summary": "periodic check",
        "plan_text": None,
        "context": context,
        "failure_tracker": {"last_failure_signature": None, "last_failure_class": None, "streak": 0},
        "claim_checks": [],
        "finalize_reason": "periodic_feedback",
        "proof_state": None,
    }

    result_state = _run_verification_rounds(
        task=SimpleNamespace(workspace_root=workspace),
        model_client=_FakeModelClient(),
        executor=executor,
        ctx=ctx,
        receipts=_FakeReceipts(tmp_path / "receipts"),
        mirror=_FakeMirror(),
        job_registry=ctx.job_registry,
        session_registry=ctx.session_registry,
        job_ids=[],
        session_ids=[],
        stated_requirements=["Do the task."],
        verifier_task_contract="Do the task.",
        seen_artifacts=set(),
        known_job_status={},
        tool_invocations=[],
        mirror_notes=[],
        discrepancy_reports=[],
        reasoning_trace_steps=[],
        step=5,
        deadline_ts=time.monotonic() + 30,
        started_at=time.monotonic(),
        orientation_dict={"cwd": str(workspace), "workspace_root": str(workspace)},
        active_tool_schemas=[],
        state=state,
        verifier_policy=None,
        completion_policy=None,
        repeat_policy=None,
    )

    assert captured["claim"] == {"summary": "periodic check", "trigger": "periodic_feedback"}
    assert isinstance(captured["action_digest"], dict)
    assert "suppressed_verifier_calls" not in result_state
    assert "completion_precheck_rejections" not in result_state


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tool_calls = []
        self.usage = {}


class _FakeModelClient:
    def call(self, messages, tools, *, cache_prefix_len=0):  # noqa: ANN001
        return _FakeResponse("REBASE_REQUEST: compact receipt continuity")


class _FakeReceipts:
    def __init__(self, root: Path) -> None:
        self.receipts_dir = root

    def record_model_exchange(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class _FakeMirror:
    streak = 0


def _tool_call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class _ProbeBudgetClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

    def call(self, messages, tools, *, cache_prefix_len=0):  # noqa: ANN001
        self.calls.append((list(messages), list(tools)))
        if len(self.calls) == 1:
            return SimpleNamespace(
                text="Need bounded inspection.",
                tool_calls=[
                    _tool_call("call_1", "read_file", {"path": "one.txt"}),
                    _tool_call("call_2", "read_file", {"path": "two.txt"}),
                    _tool_call("call_3", "run_command", {"cmd": "ls"}),
                    _tool_call("call_4", "read_file", {"path": "four.txt"}),
                    _tool_call("call_5", "unknown_tool", {}),
                ],
            )
        return SimpleNamespace(
            text=json.dumps(
                {
                    "requirements": [
                        {
                            "requirement": "Inspect workspace",
                            "verdict": "unsatisfied",
                            "evidence": "Inspection budget was exhausted before all requested reads.",
                            "evidence_refs": [
                                "inspection.read_file[0]",
                                "inspection.read_file[2]",
                                "inspection.unknown_tool[0]",
                            ],
                        }
                    ],
                    "reason_codes": ["verification_inspection_budget_exhausted"],
                    "summary": "bounded verifier inspection stopped extra probes",
                }
            ),
            tool_calls=[],
        )


class _ProbeBudgetInspectionContext:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, object]]] = []

    def read_file(self, **arguments):  # noqa: ANN003
        self.executed.append(("read_file", dict(arguments)))
        return {"exit_code": 0, "cwd": "/workspace", "stdout": f"read {arguments.get('path')}", "stderr": ""}

    def run_command(self, **arguments):  # noqa: ANN003
        self.executed.append(("run_command", dict(arguments)))
        return {"exit_code": 0, "cwd": "/workspace", "stdout": "listed", "stderr": ""}


def test_verifier_inspection_calls_are_bounded_and_all_tool_calls_answered() -> None:
    client = _ProbeBudgetClient()
    inspection_ctx = _ProbeBudgetInspectionContext()

    report = verify_fresh_context(
        "Inspect workspace",
        {"cwd": "/workspace"},
        {"added_paths": []},
        {"summary": "claim"},
        [],
        {"tool_calls": []},
        client,
        inspection_ctx=inspection_ctx,
        max_inspection_calls=3,
    )

    assert [name for name, _ in inspection_ctx.executed] == ["read_file", "read_file", "run_command"]
    assert len(client.calls) == 2
    second_messages = client.calls[1][0]
    tool_messages = [message for message in second_messages if message.get("role") == "tool"]
    assert [message.get("tool_call_id") for message in tool_messages] == [
        "call_1",
        "call_2",
        "call_3",
        "call_4",
        "call_5",
    ]
    rendered = json.dumps(tool_messages, sort_keys=True)
    assert "verification_inspection_budget_exhausted" in rendered
    assert "verification_unknown_tool" in rendered
    assert report.reason_codes == ("verification_inspection_budget_exhausted",)


def test_read_only_verification_context_rejects_mutating_commands_and_records(tmp_path: Path) -> None:
    recorded: list[tuple[int, str, dict[str, object], object]] = []

    class _Receipts:
        def record_verifier_command(self, call_idx, tool_name, arguments, envelope):  # noqa: ANN001
            recorded.append((call_idx, tool_name, dict(arguments), envelope))

    ctx = SimpleNamespace(
        executor=SimpleNamespace(workspace_root=tmp_path),
        raw_log_dir=tmp_path / "raw",
        run_command=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mutating command executed")),
    )
    verifier_ctx = _ReadOnlyVerificationContext(ctx, _Receipts())

    envelope = verifier_ctx.run_command("touch should_not_exist.txt")

    assert envelope.exit_code == 1
    assert envelope.error is not None
    assert envelope.error.reason_code == "verification_read_only_violation"
    assert not (tmp_path / "should_not_exist.txt").exists()
    assert recorded
    assert recorded[0][1] == "run_command"


def test_verification_round_rebase_preserves_receipt_continuity(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = ContainerExecutor(workspace_root=workspace)
    ctx = ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(tmp_path / "state", backend=executor.backend, container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(tmp_path / "state", backend=executor.backend),
        raw_log_dir=tmp_path / "raw",
    )
    receipt_store = QueryableReceiptStore(root=tmp_path, run_id="proof")
    receipt_store.set_success_contract({"contract_text": "must pass"})
    receipt_store.update_plan(step=1, plan_text="PLAN_UPDATE:\n- [pending] Inspect\n", reason="test")
    local_tools = TaskLocalToolRegistry(root=tmp_path)
    ctx.receipt_store = receipt_store
    ctx.task_local_tools = local_tools
    ctx.receipt_context_pack_policy = ContextPackPolicy()

    context = ContextManager(delta_state=ctx.last_snapshot)
    context.build_prefix(
        system_prompt="kernel",
        task_instruction="Do the task.",
        orientation={"cwd": str(workspace), "workspace_root": str(workspace)},
        tool_schemas=[],
    )
    context.set_completion_contract({"current_unresolved_requirement": "Inspect", "required_final_evidence": []})

    captured: dict[str, object] = {}

    def _fake_verify(*args, **kwargs):  # noqa: ANN002, ANN003
        return _report("Need more evidence.", "still blocked", reason_codes=("needs_more",))

    def _fake_monitor(**kwargs):  # noqa: ANN003
        return {"applies": False}, kwargs["start_snapshot"]

    def _fake_rebase(context_obj, model_client, **kwargs):  # noqa: ANN001
        captured["snapshot"] = kwargs.get("receipt_continuity_snapshot")
        return context_obj

    monkeypatch.setattr("harness.aether2.control.verification_rounds.verify_fresh_context", _fake_verify)
    monkeypatch.setattr("harness.aether2.control.verification_rounds._monitor_persistent_runtime", _fake_monitor)
    monkeypatch.setattr("harness.aether2.control.verification_rounds.rebase", _fake_rebase)

    state = {
        "verification_rounds": 0,
        "verification_round_limit": 1,
        "feedback_only": False,
        "model_calls": 0,
        "tokens_cached": 0,
        "tokens_fresh": 0,
        "total_cost": 0.0,
        "compaction_count": 0,
        "recoveries": 0,
        "no_delta_streaks": 0,
        "finalize_pass": False,
        "finalize_summary": "claim",
        "plan_text": "PLAN_UPDATE:\n- [pending] Inspect\n",
        "context": context,
        "failure_tracker": {"last_failure_signature": None, "last_failure_class": None, "streak": 0},
        "claim_checks": [],
        "finalize_reason": "verification_requested",
        "proof_state": {"state": "not_ready", "summary": "requirements=1"},
    }

    _run_verification_rounds(
        task=SimpleNamespace(workspace_root=workspace),
        model_client=_FakeModelClient(),
        executor=executor,
        ctx=ctx,
        receipts=_FakeReceipts(tmp_path / "receipts"),
        mirror=_FakeMirror(),
        job_registry=ctx.job_registry,
        session_registry=ctx.session_registry,
        job_ids=[],
        session_ids=[],
        stated_requirements=["Do the task."],
        verifier_task_contract="Do the task.",
        seen_artifacts=set(),
        known_job_status={},
        tool_invocations=[],
        mirror_notes=[],
        discrepancy_reports=[],
        reasoning_trace_steps=[],
        step=1,
        deadline_ts=time.monotonic() + 30,
        started_at=time.monotonic(),
        orientation_dict={"cwd": str(workspace), "workspace_root": str(workspace)},
        active_tool_schemas=[],
        state=state,
        verifier_policy=None,
        completion_policy=None,
        repeat_policy=None,
    )

    snapshot = captured.get("snapshot")
    assert isinstance(snapshot, dict)
    assert "proof_state" in snapshot
    assert "plan" in snapshot


def test_task_done_evidence_floor_is_verifier_evidence_not_precheck_veto(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = ContainerExecutor(workspace_root=workspace)
    ctx = ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(tmp_path / "state", backend=executor.backend, container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(tmp_path / "state", backend=executor.backend),
        raw_log_dir=tmp_path / "raw",
    )
    context = ContextManager(delta_state=ctx.last_snapshot)
    context.build_prefix(
        system_prompt="kernel",
        task_instruction="Do the task.",
        orientation={"cwd": str(workspace), "workspace_root": str(workspace)},
        tool_schemas=[],
    )
    captured: dict[str, object] = {}

    def _fake_verify(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["action_digest"] = args[5]
        return _report(
            "Completion needs evidence.",
            "task_done had no replayed checks or independent runtime evidence",
            reason_codes=("completion_evidence_gate_rejected",),
        )

    def _fake_monitor(**kwargs):  # noqa: ANN003
        return {"applies": False}, kwargs["start_snapshot"]

    monkeypatch.setattr("harness.aether2.control.verification_rounds.verify_fresh_context", _fake_verify)
    monkeypatch.setattr("harness.aether2.control.verification_rounds._monitor_persistent_runtime", _fake_monitor)

    state = {
        "verification_rounds": 0,
        "verification_round_limit": 1,
        "feedback_only": True,
        "model_calls": 0,
        "tokens_cached": 0,
        "tokens_fresh": 0,
        "total_cost": 0.0,
        "compaction_count": 0,
        "recoveries": 0,
        "no_delta_streaks": 0,
        "finalize_pass": False,
        "finalize_summary": "done",
        "plan_text": None,
        "context": context,
        "failure_tracker": {"last_failure_signature": None, "last_failure_class": None, "streak": 0},
        "claim_checks": [],
        "finalize_reason": "task_done",
        "proof_state": None,
    }

    result_state = _run_verification_rounds(
        task=SimpleNamespace(workspace_root=workspace),
        model_client=_FakeModelClient(),
        executor=executor,
        ctx=ctx,
        receipts=_FakeReceipts(tmp_path / "receipts"),
        mirror=_FakeMirror(),
        job_registry=ctx.job_registry,
        session_registry=ctx.session_registry,
        job_ids=[],
        session_ids=[],
        stated_requirements=["Do the task."],
        verifier_task_contract="Do the task.",
        seen_artifacts=set(),
        known_job_status={},
        tool_invocations=[],
        mirror_notes=[],
        discrepancy_reports=[],
        reasoning_trace_steps=[],
        step=1,
        deadline_ts=time.monotonic() + 30,
        started_at=time.monotonic(),
        orientation_dict={"cwd": str(workspace), "workspace_root": str(workspace)},
        active_tool_schemas=[],
        state=state,
        verifier_policy=None,
        completion_policy=None,
        repeat_policy=None,
    )

    action_digest = captured["action_digest"]
    assert isinstance(action_digest, dict)
    assert action_digest["completion_runtime_floor"]["status"] == "evidence_floor_warning"
    assert action_digest["completion_runtime_floor"]["reason_codes"] == ["completion_evidence_gate_rejected"]
    assert "completion_precheck_rejections" not in result_state


def test_verifier_inspection_payload_redacts_host_run_metadata() -> None:
    payload = _inspection_payload(
        {
            "exit_code": 1,
            "cwd": (
                "/tmp/harbor-jobs/run/"
                "qemu-startup-receipt_driven_full-rep1/agent/tmp/harbor_workspace_mirror"
            ),
            "stdout_head": (
                "inspected task_id=qemu-startup condition=receipt_driven_full "
                "benchmark=terminal-bench suite=official_tasks"
            ),
            "stderr_head": (
                "log=/tmp/harbor-jobs/run/"
                "qemu-startup-receipt_driven_full-rep1/model_exchange_18.json"
            ),
            "error": {
                "message": (
                    "task_id=qemu-startup benchmark=terminal-bench "
                    "run_id=qemu-startup__receipt_driven_full"
                )
            },
        }
    )

    rendered = str(payload)
    for forbidden in (
        "qemu-startup",
        "receipt_driven_full",
        "terminal-bench",
        "official_tasks",
        "/tmp/harbor-jobs/",
    ):
        assert forbidden not in rendered
    assert payload["cwd"] == "[host_run_path]"
    assert "[redacted_metadata]" in rendered
