from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from harness.aether2.control.execution_context import ToolInvocationRecord
from harness.aether2.control.loop import ExecutionContext, run_aether2_loop
from harness.aether2.control.requirements import _extract_stated_requirements
from harness.aether2.hooks import HookRegistry, HookResult, PermissionDecision, PermissionDecisionReason
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.model_client import ModelResponse
from harness.aether2.runtime.prompts import SYSTEM_PROMPT
from harness.aether2.runtime.run_config import build_baseline_run_config
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.runtime.task_spec import TaskSpec
from harness.aether2.tools.native import dispatch_with_hooks


def _make_execution_context(tmp_path: Path, hook_registry: HookRegistry | None = None) -> tuple[ExecutionContext, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    state_dir = tmp_path / "state"
    raw_log_dir = tmp_path / "raw_logs"
    executor = ContainerExecutor(workspace_root=workspace_root)
    job_registry = JobRegistry(state_dir, backend=executor.backend, container_path_fn=executor.to_container_path)
    session_registry = SessionRegistry(state_dir, backend=executor.backend)
    ctx = ExecutionContext(
        executor=executor,
        job_registry=job_registry,
        session_registry=session_registry,
        raw_log_dir=raw_log_dir,
        hook_registry=hook_registry,
    )
    return ctx, workspace_root


def _response(text: str = "", tool_calls: tuple[dict, ...] = ()) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=tool_calls,
        usage={"cached_input_tokens": 0, "fresh_input_tokens": 0},
        status="completed",
        raw_response={},
    )


def _tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"id": call_id, "type": "function", "name": name, "arguments": json.dumps(arguments)}


def _verify_response() -> ModelResponse:
    payload = {
        "requirements": [
            {
                "requirement": "task complete",
                "verdict": "satisfied",
                "evidence": "checked",
                "evidence_refs": ["checks_results[0]"],
            }
        ],
        "reason_codes": [],
        "summary": "ok",
    }
    return _response(text=json.dumps(payload))


class _ScriptedModelClient:
    def __init__(self, turns: list[ModelResponse]) -> None:
        self.turns = list(turns)

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        del tools
        if any("fresh-context verifier" in str(message.get("content", "")) for message in messages):
            return _verify_response()
        if self.turns:
            return self.turns.pop(0)
        return _response(text="done")


class _RecordingModelClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        self.calls.append({"message_count": len(messages), "tool_count": len(tools)})
        return _response(text="stopping without tools")


class _VerifierFeedbackModelClient:
    def __init__(self) -> None:
        self.normal_calls = 0
        self.normal_message_texts: list[str] = []

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        del cache_prefix_len
        tool_names = {
            tool.get("function", {}).get("name")
            for tool in tools
            if isinstance(tool, dict)
        }
        if tool_names and tool_names.issubset({"run_command", "read_file", "job_status", "session_read"}):
            payload = {
                "requirements": [
                    {
                        "requirement": "write out.txt",
                        "verdict": "unverifiable",
                        "evidence": "No file evidence was gathered.",
                        "unresolved": True,
                    }
                ],
                "reason_codes": ["missing_artifact_evidence"],
                "summary": "need file evidence",
            }
            return _response(text=json.dumps(payload))
        self.normal_calls += 1
        self.normal_message_texts.append("\n".join(str(message.get("content", "")) for message in messages))
        if self.normal_calls == 1:
            return _response(
                text="premature done",
                tool_calls=(_tool_call("task_done", {"summary": "done", "checks": []}),),
            )
        return _response(text="blocked after verifier feedback")


class _ImplicitStopVerifierFeedbackClient:
    def __init__(self) -> None:
        self.normal_calls = 0
        self.verifier_calls = 0
        self.repair_calls = 0
        self.repair_message_text = ""

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        del cache_prefix_len
        tool_names = {
            tool.get("function", {}).get("name")
            for tool in tools
            if isinstance(tool, dict)
        }
        joined = "\n".join(str(message.get("content", "")) for message in messages)
        if tool_names and tool_names.issubset({"run_command", "read_file", "job_status", "session_read"}):
            self.verifier_calls += 1
            payload = {
                "requirements": [
                    {
                        "requirement": "write out.txt",
                        "verdict": "unsatisfied",
                        "evidence": "No file was written.",
                        "evidence_refs": ["workspace_diff"],
                    }
                ],
                "reason_codes": ["missing_artifact_evidence"],
                "summary": "out.txt is still missing",
            }
            return _response(text=json.dumps(payload))
        if "verification_blocker" in joined:
            self.repair_calls += 1
            self.repair_message_text = joined
            return _response(
                text="repair after verifier feedback",
                tool_calls=(_tool_call("write_file", {"path": "out.txt", "content": "hello"}, call_id="call-repair"),),
            )
        self.normal_calls += 1
        return _response(text="stopping without tools")


def _make_task(tmp_path: Path, instruction: str = "write the file") -> TaskSpec:
    task_dir = tmp_path / "task"
    workspace_root = task_dir / "workspace"
    artifacts_dir = task_dir / "artifacts"
    workspace_root.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)
    return TaskSpec(
        task_id="hook-smoke-task",
        instruction=instruction,
        task_dir=task_dir,
        workspace_root=workspace_root,
        artifacts_dir=artifacts_dir,
    )


def _freeze_identity_surfaces(monkeypatch) -> None:  # noqa: ANN001
    monotonic_tick = {"value": 0}

    def fake_monotonic() -> float:
        monotonic_tick["value"] += 1
        return float(monotonic_tick["value"])

    perf_tick = {"value": 0}

    def fake_perf_counter() -> float:
        perf_tick["value"] += 1
        return float(perf_tick["value"])

    uuid_tick = {"value": 0}

    class _StaticUUID:
        def __init__(self, hex_value: str) -> None:
            self.hex = hex_value

    def fake_uuid4() -> _StaticUUID:
        uuid_tick["value"] += 1
        return _StaticUUID(f"{uuid_tick['value']:032x}")

    import harness.aether2.control.execution_context as execution_context_module
    import harness.aether2.control.loop as loop_module
    import harness.aether2.control.verification_rounds as verification_rounds_module
    import harness.aether2.runtime.executor as executor_module
    import harness.aether2.traces.envelope as envelope_module
    from harness.aether2.runtime.orientation import OrientationSnapshot

    monkeypatch.setattr(execution_context_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(loop_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(verification_rounds_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(executor_module, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(envelope_module.uuid, "uuid4", fake_uuid4)

    # Freeze orientation snapshot so live network state does not break byte-identity
    _frozen_orientation = OrientationSnapshot(
        cwd="/frozen/workspace",
        user="frozen-user",
        is_root=False,
        workspace_root="/frozen/workspace",
        writable_paths=["/frozen/workspace"],
        safe_file_listing=[],
        tool_presence={},
        package_managers={},
        network="unreachable",
        network_reachable=False,
        network_evidence="frozen",
        runtimes={},
        processes=[],
        ports=[],
        env_contract_version="frozen-v0",
        env_contract_digest="frozen-digest",
        env_contract={},
    )
    monkeypatch.setattr(loop_module, "orient", lambda _executor: _frozen_orientation)
    monkeypatch.setattr(verification_rounds_module, "orient", lambda _executor: _frozen_orientation)


def _host_receipt_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_dispatch_with_hooks_runs_permission_pre_and_post_hooks_in_order(tmp_path: Path) -> None:
    order: list[str] = []
    registry = HookRegistry()

    def permission_hook(_context):
        order.append("permission")
        return HookResult(note="permission")

    def pre_hook(_context):
        order.append("pre")
        return HookResult(note="pre")

    def post_hook(_context):
        order.append("post")
        return HookResult(note="post")

    registry.register("permission_request", hook_name="permission", callback=permission_hook)
    registry.register("pre_tool_use", hook_name="pre", callback=pre_hook)
    registry.register("post_tool_use", hook_name="post", callback=post_hook)

    ctx, workspace_root = _make_execution_context(tmp_path, hook_registry=registry)
    outcome = dispatch_with_hooks(
        "write_file",
        {"path": "out.txt", "content": "hello"},
        ctx,
        call_id="call-1",
    )

    assert order == ["permission", "pre", "post"]
    assert (workspace_root / "out.txt").read_text(encoding="utf-8") == "hello"
    assert outcome.permission_decision == {
        "behavior": "allow",
        "message": None,
        "reason": {"type": "default", "source": "policy", "hook_name": None, "message": None, "rule_id": None},
        "updated_input": None,
        "metadata": {},
    }
    assert [entry["event"] for entry in outcome.hook_trace] == [
        "permission_request",
        "pre_tool_use",
        "post_tool_use",
    ]


def test_task_done_accepts_schema_advertised_limitations_and_requirements(tmp_path: Path) -> None:
    ctx, _workspace_root = _make_execution_context(tmp_path)

    outcome = dispatch_with_hooks(
        "task_done",
        {
            "summary": "done",
            "checks": ["python3 checks/visible_check.py --candidate out/final_submission.json"],
            "requirements": [
                {
                    "requirement": "write the final submission artifact",
                    "check": "python3 checks/visible_check.py --candidate out/final_submission.json",
                    "known_limitations": ["verifier output not yet inspected"],
                }
            ],
            "limitations": ["artifact not independently verified yet"],
        },
        ctx,
        call_id="call-task-done",
    )

    assert outcome.envelope.exit_code == 0
    assert outcome.envelope.error is None


def test_start_job_rejects_terminal_interactive_commands_with_session_guidance(tmp_path: Path) -> None:
    ctx, _workspace_root = _make_execution_context(tmp_path)

    envelope = ctx.start_job(
        "qemu-system-x86_64 -nographic -serial mon:stdio -drive file=disk.qcow2",
        job_id="interactive",
    )

    assert envelope.exit_code == 126
    assert envelope.error is not None
    assert envelope.error.reason_code == "interactive_job_requires_session_start"
    assert "Use session_start" in envelope.stderr_head


def test_start_job_still_allows_noninteractive_background_commands(tmp_path: Path) -> None:
    ctx, _workspace_root = _make_execution_context(tmp_path)

    envelope = ctx.start_job("sleep 1", job_id="sleeper")

    assert envelope.exit_code == 0
    assert envelope.error is None
    assert "started job sleeper" in envelope.stdout_head


def test_task_blocked_is_supported_as_terminal_claim(tmp_path: Path) -> None:
    ctx, _workspace_root = _make_execution_context(tmp_path)

    outcome = dispatch_with_hooks(
        "task_blocked",
        {
            "blocker": "visible verifier still blocked",
            "evidence": ["command hit workspace boundary guard"],
            "attempts": ["retried with workspace-relative reads"],
            "missing_external_state": ["safe verifier execution path"],
            "recommended_next_evidence": ["rerun verifier after artifact write succeeds"],
        },
        ctx,
        call_id="call-task-blocked",
    )

    assert outcome.envelope.exit_code == 0
    assert outcome.envelope.error is None
    assert outcome.envelope.stdout_head == "visible verifier still blocked"


def test_query_evidence_searches_current_run_and_query_history_alias_still_works(tmp_path: Path) -> None:
    ctx, _workspace_root = _make_execution_context(tmp_path)

    write_outcome = dispatch_with_hooks(
        "write_file",
        {"path": "out.txt", "content": "hello evidence"},
        ctx,
        call_id="call-write",
    )
    ctx._run_tool_invocations.append(
        ToolInvocationRecord(
            step=1,
            tool_name="write_file",
            arguments={"path": "out.txt", "content": "hello evidence"},
            envelope=write_outcome.envelope,
        )
    )

    outcome = dispatch_with_hooks(
        "query_evidence",
        {"query": "hello evidence"},
        ctx,
        call_id="call-query",
    )

    assert outcome.envelope.exit_code == 0
    assert "query_evidence: 1 result" in outcome.envelope.stdout_head
    assert "write_file" in outcome.envelope.stdout_head

    legacy_outcome = dispatch_with_hooks(
        "query_history",
        {"query": "hello evidence"},
        ctx,
        call_id="call-query-legacy",
    )

    assert legacy_outcome.envelope.exit_code == 0
    assert "query_evidence: 1 result" in legacy_outcome.envelope.stdout_head


def test_denied_action_is_visible_and_does_not_mutate_workspace(tmp_path: Path) -> None:
    order: list[str] = []
    registry = HookRegistry()

    def permission_hook(_context):
        order.append("permission")
        return HookResult(
            permission_decision=PermissionDecision(
                behavior="deny",
                message="blocked by hook",
                reason=PermissionDecisionReason(type="hook", source="test", hook_name="deny_write"),
            ),
            note="deny",
        )

    def pre_hook(_context):
        order.append("pre")
        return HookResult(note="pre")

    def post_hook(context):
        order.append("post")
        assert context.permission_decision is not None
        assert context.permission_decision.behavior == "deny"
        return HookResult(note="post")

    registry.register("permission_request", hook_name="deny_write", callback=permission_hook)
    registry.register("pre_tool_use", hook_name="pre", callback=pre_hook)
    registry.register("post_tool_use", hook_name="post", callback=post_hook)

    ctx, workspace_root = _make_execution_context(tmp_path, hook_registry=registry)
    outcome = dispatch_with_hooks(
        "write_file",
        {"path": "out.txt", "content": "hello"},
        ctx,
        call_id="call-1",
    )

    assert order == ["permission", "post"]
    assert not (workspace_root / "out.txt").exists()
    assert outcome.envelope.exit_code == 1
    assert outcome.envelope.error is not None
    assert outcome.envelope.error.reason_code == "tool_permission_denied"
    assert outcome.envelope.files_changed == []
    assert "blocked by hook" in outcome.envelope.stderr_head


def test_permission_argument_mutation_is_denied_in_first_port_slice(tmp_path: Path) -> None:
    registry = HookRegistry()
    original_arguments = {"path": "out.txt", "content": "hello"}

    def mutating_permission_hook(context):
        mutated = dict(context.arguments)
        mutated["path"] = "rewritten.txt"
        return HookResult(
            permission_decision=PermissionDecision(
                behavior="allow",
                updated_input=mutated,
                reason=PermissionDecisionReason(type="hook", source="test", hook_name="mutate"),
            ),
            note="mutate",
        )

    registry.register("permission_request", hook_name="mutate", callback=mutating_permission_hook)
    ctx, workspace_root = _make_execution_context(tmp_path, hook_registry=registry)
    outcome = dispatch_with_hooks("write_file", original_arguments, ctx, call_id="call-1")

    assert original_arguments == {"path": "out.txt", "content": "hello"}
    assert not (workspace_root / "out.txt").exists()
    assert not (workspace_root / "rewritten.txt").exists()
    assert outcome.permission_decision is not None
    assert outcome.permission_decision["behavior"] == "deny"
    assert "mutation" in (outcome.permission_decision["message"] or "").lower()


def test_hook_and_permission_exports_are_public() -> None:
    import harness.aether2 as public_api

    assert public_api.HookRegistry is HookRegistry
    assert public_api.PermissionDecision is PermissionDecision
    assert public_api.PermissionManager is not None


def test_loop_records_hook_and_permission_metadata_in_trace_and_receipts(tmp_path: Path) -> None:
    registry = HookRegistry()
    registry.register("permission_request", hook_name="permission", callback=lambda _context: HookResult(note="permission"))
    registry.register("pre_tool_use", hook_name="pre", callback=lambda _context: HookResult(note="pre"))
    registry.register("post_tool_use", hook_name="post", callback=lambda _context: HookResult(note="post"))

    task = _make_task(tmp_path)
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _ScriptedModelClient(
        [
            _response(
                text="write",
                tool_calls=(_tool_call("write_file", {"path": "out.txt", "content": "hello"}),),
            ),
            _response(
                text="done",
                tool_calls=(_tool_call("task_done", {"summary": "done", "checks": ["cat out.txt"]}, call_id="call-2"),),
            ),
        ]
    )

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 60,
        hook_registry=registry,
    )

    assert result.finalize_reason == "task_done"
    assert result.tool_invocations[0].permission_decision is not None
    assert result.tool_invocations[0].permission_decision["behavior"] == "allow"
    assert [entry["event"] for entry in result.tool_invocations[0].hook_trace] == [
        "permission_request",
        "pre_tool_use",
        "post_tool_use",
    ]

    receipt_path = task.task_dir / ".aether2" / "host_receipts" / "receipts" / "0001_action_write_file.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["action"]["permission_decision"]["behavior"] == "allow"
    assert [entry["event"] for entry in receipt["action"]["hook_trace"]] == [
        "permission_request",
        "pre_tool_use",
        "post_tool_use",
    ]

    trace = json.loads(Path(result.reasoning_trace_ref).read_text(encoding="utf-8"))
    assert trace["steps"][0]["tool_calls"][0]["permission_decision"]["behavior"] == "allow"
    assert [entry["event"] for entry in trace["steps"][0]["tool_calls"][0]["hook_trace"]] == [
        "permission_request",
        "pre_tool_use",
        "post_tool_use",
    ]


def test_loop_accepts_monotonic_deadline_before_first_model_turn(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _RecordingModelClient()

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 60,
    )

    assert result.finalize_reason == "implicit_stop"
    assert client.calls
    assert client.calls[0]["tool_count"] > 0


def test_loop_marks_deadline_before_first_turn_without_model_call(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _RecordingModelClient()

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() - 1,
    )

    assert result.finalize_reason == "deadline_before_first_turn"
    assert result.model_calls == 0
    assert client.calls == []


def test_rejected_task_done_feedback_returns_to_normal_loop_before_repair_call(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="write out.txt")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _VerifierFeedbackModelClient()

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 60,
    )

    assert result.finalize_reason in {"implicit_stop", "task_blocked"}
    assert result.verification_rounds >= 1
    assert client.normal_calls >= 2
    receipt_dir = task.task_dir / ".aether2" / "host_receipts" / "receipts"
    roles = [
        json.loads(path.read_text(encoding="utf-8"))["call_role"]
        for path in sorted(receipt_dir.glob("model_exchange_*.json"))
    ]
    assert roles[:3] == ["normal", "verifier", "normal"]
    assert roles.count("normal") == 2


def test_verifier_feedback_receipt_store_is_variant_gated(tmp_path: Path) -> None:
    def run_case(root: Path, *, receipt_variant: bool) -> tuple[object, Path]:
        task = _make_task(root, instruction="write out.txt")
        executor = ContainerExecutor(workspace_root=task.workspace_root)
        client = _VerifierFeedbackModelClient()
        result = run_aether2_loop(
            task,
            client,
            executor,
            deadline_ts=time.monotonic() + 60,
            receipt_driven_variant_enabled=receipt_variant,
        )
        return result, task.workspace_root / ".aether2" / "receipt_store"

    result_off, off_store = run_case(tmp_path / "off", receipt_variant=False)
    result_on, on_store = run_case(tmp_path / "on", receipt_variant=True)

    assert result_off.verification_rounds >= 1
    assert result_on.verification_rounds >= 1
    assert off_store.exists() is False
    assert on_store.exists() is True

    events_text = (on_store / "events.jsonl").read_text(encoding="utf-8")
    assert "verification_feedback" in events_text
    assert "verification blocked" in events_text


def test_flag_off_default_task_done_path_matches_explicit_baseline_receipts_byte_for_byte(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instruction = "finish the task"

    def run_case(*, explicit_baseline: bool) -> tuple[object, dict[str, bytes]]:
        _freeze_identity_surfaces(monkeypatch)
        task = _make_task(tmp_path, instruction=instruction)
        executor = ContainerExecutor(workspace_root=task.workspace_root)
        client = _ScriptedModelClient(
            [
                _response(
                    text="done",
                    tool_calls=(
                        _tool_call("task_done", {"summary": "done", "checks": ["true"]}),
                    ),
                ),
            ]
        )
        run_config = None
        if explicit_baseline:
            baseline_ctx, _ = _make_execution_context(tmp_path / "baseline_seed")
            run_config = build_baseline_run_config(
                system_prompt=SYSTEM_PROMPT,
                base_tool_schemas=baseline_ctx.tool_registry.tool_schemas(),
                base_stated_requirements=_extract_stated_requirements(instruction),
            )
            shutil.rmtree((tmp_path / "baseline_seed"), ignore_errors=True)
        result = run_aether2_loop(
            task,
            client,
            executor,
            deadline_ts=120.0,
            adaptive_profile_enabled=False,
            receipt_driven_variant_enabled=False,
            run_config=run_config,
        )
        receipt_root = task.task_dir / ".aether2" / "host_receipts"
        return result, _host_receipt_bytes(receipt_root)

    result_off, receipts_off = run_case(explicit_baseline=False)
    shutil.rmtree(tmp_path / "task")
    result_baseline, receipts_baseline = run_case(explicit_baseline=True)

    assert result_off.finalize_reason == "task_done"
    assert result_baseline.finalize_reason == "task_done"
    assert result_off.model_calls == result_baseline.model_calls
    assert receipts_off == receipts_baseline


def test_implicit_stop_verifier_feedback_gets_repair_turn(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="write out.txt")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _ImplicitStopVerifierFeedbackClient()

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 60,
    )

    assert client.verifier_calls == 1
    assert client.repair_calls == 1
    assert "out.txt is still missing" in client.repair_message_text
    assert "Do not merely acknowledge this verifier feedback." in client.repair_message_text
    assert (task.workspace_root / "out.txt").read_text(encoding="utf-8") == "hello"
    assert result.verification_rounds == 1
    receipt_dir = task.task_dir / ".aether2" / "host_receipts" / "receipts"
    roles = [
        json.loads(path.read_text(encoding="utf-8"))["call_role"]
        for path in sorted(receipt_dir.glob("model_exchange_*.json"))
    ]
    assert roles == ["normal", "verifier", "repair"]
