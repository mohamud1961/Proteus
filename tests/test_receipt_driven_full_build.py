from __future__ import annotations

import json
from pathlib import Path

from harness.aether2.control.execution_context import ExecutionContext
from harness.aether2.control.candidate_preservation import CandidatePreservation
from harness.aether2.control.completion import _build_proof_state
from harness.aether2.control.action_helpers import _envelope_to_message
from harness.aether2.runtime.context import ContextManager, sanitize_model_visible_payload
from harness.aether2.runtime.adaptive_profile_helpers import solver_visible_orientation
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.run_config import ContextPackPolicy, make_harness_run_config
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.tools.native import dispatch
from harness.aether2.traces.failure_cards import build_failure_card, classify_failure
from harness.aether2.traces.envelope import build_envelope
from harness.aether2.traces.receipt_store import QueryableReceiptStore, parse_plan_update
from harness.aether2.traces.task_local_tools import TaskLocalToolRegistry


def _ctx(tmp_path: Path) -> tuple[ExecutionContext, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    executor = ContainerExecutor(workspace_root=workspace)
    return (
        ExecutionContext(
            executor=executor,
            job_registry=JobRegistry(state, backend=executor.backend, container_path_fn=executor.to_container_path),
            session_registry=SessionRegistry(state, backend=executor.backend),
            raw_log_dir=tmp_path / "raw",
        ),
        workspace,
    )


def test_parse_plan_update_marks_done_without_evidence_missing() -> None:
    items = parse_plan_update(
        "notes\nPLAN_UPDATE:\n- [done] Inspect input files\n- [in_progress] Build parser\n- [weird] Create output\n",
    )

    assert items[0]["status"] == "done"
    assert items[0]["evidence_missing"] is True
    assert items[1]["status"] == "in_progress"
    assert items[2]["status"] == "pending"


def test_receipt_store_plan_json_preserves_prior_items_and_context(tmp_path: Path) -> None:
    store = QueryableReceiptStore(root=tmp_path, run_id="t")
    store.update_plan(step=1, plan_text="PLAN_UPDATE:\n- [pending] Inspect\n", reason="test")
    store.update_plan(step=2, plan_text="PLAN_UPDATE:\n- [done] Inspect\n- [blocked] Validate\n", reason="test")

    plan = json.loads((tmp_path / ".aether2/receipt_store/plan.json").read_text())
    assert [item["text"] for item in plan["items"]] == ["Inspect", "Validate"]
    assert plan["items"][0]["status"] == "done"
    assert plan["items"][0]["evidence_missing"] is True
    context = store.context_view(policy=make_harness_run_config(system_prompt="s", active_tool_schemas=[]).context_pack)
    assert "plan" in context


def test_query_evidence_searches_receipt_store_and_local_tools(tmp_path: Path) -> None:
    ctx, _workspace = _ctx(tmp_path)
    store = QueryableReceiptStore(root=tmp_path, run_id="t")
    local_tools = TaskLocalToolRegistry(root=tmp_path)
    ctx.receipt_store = store
    ctx.task_local_tools = local_tools
    event = store.record_tool_result(
        step=3,
        tool_name="run_command",
        arguments={"cmd": "pytest tests"},
        exit_code=0,
        stdout="PASSED sentinel",
        stderr="",
        raw_log_path="/tmp/raw.json",
        files_changed=["out.txt"],
    )
    local_tools.observe_tool_invocation(
        step=4,
        tool_name="write_file",
        arguments={"path": ".aether/tools/helper.py"},
        exit_code=0,
        evidence_id=event.event_id,
        files_changed=[".aether/tools/helper.py"],
    )

    result = ctx.query_evidence("sentinel").stdout_head
    assert "receipt_store" in result
    assert event.event_id in result
    assert "no matching" in ctx.query_evidence("missing-needle").stdout_head


def test_receipt_store_records_run_telemetry(tmp_path: Path) -> None:
    store = QueryableReceiptStore(root=tmp_path, run_id="telemetry")
    store.record_run_telemetry(
        step=7,
        model_calls=11,
        tokens_cached=1200,
        tokens_fresh=340,
        latency_sec=8.25,
        no_progress_streak=2,
        proof_state_delta=1,
        cost_usd=0.0042,
        proof_state={
            "state": "weak",
            "summary": "Receipt-backed evidence exists, but it is still thin.",
            "score": 1,
            "delta": 1,
            "rejected_proxy_evidence": [".aether/tools/checker.py: task-local helper is not trusted for completion"],
        },
        rejected_proxy_evidence=[".aether/tools/checker.py: task-local helper is not trusted for completion"],
    )

    event = store.query("run telemetry", event_type="run_telemetry")[0]
    assert event["payload"]["tokens_cached"] == 1200
    assert event["payload"]["tokens_fresh"] == 340
    assert event["payload"]["no_progress_streak"] == 2
    assert event["payload"]["proof_state_delta"] == 1
    assert event["payload"]["proof_state"]["state"] == "weak"
    assert event["payload"]["rejected_proxy_evidence"]
    assert event["payload"]["cost_usd"] == 0.0042


def test_task_local_tool_trust_gate(tmp_path: Path) -> None:
    registry = TaskLocalToolRegistry(root=tmp_path)
    registry.observe_tool_invocation(
        step=1,
        tool_name="write_file",
        arguments={"path": ".aether/tools/checker.py"},
        exit_code=0,
        evidence_id="evt1",
        files_changed=[".aether/tools/checker.py"],
    )
    assert registry.summary()["tools"][0]["trusted_for_completion"] is False
    registry.observe_tool_invocation(
        step=2,
        tool_name="run_command",
        arguments={"cmd": "python .aether/tools/checker.py --self-test"},
        exit_code=0,
        evidence_id="evt2",
        files_changed=[],
    )
    tool = registry.summary()["tools"][0]
    assert tool["trusted_for_completion"] is True
    assert tool["trusted_for_current_run"] is True
    assert tool["validated"] is True
    assert any("trusted" in note for note in tool["notes"])
    assert tool["last_used_step"] == 2


def test_task_local_tool_validation_tracks_job_and_session_launches(tmp_path: Path) -> None:
    registry = TaskLocalToolRegistry(root=tmp_path)
    registry.observe_tool_invocation(
        step=1,
        tool_name="start_job",
        arguments={"cmd": "python .aether/tools/helper.py --self-test"},
        exit_code=0,
        evidence_id="evt_job",
        files_changed=[],
    )
    registry.observe_tool_invocation(
        step=2,
        tool_name="session_start",
        arguments={"command": "python .aether/tools/helper.py"},
        exit_code=0,
        evidence_id="evt_session",
        files_changed=[],
    )

    tool = registry.summary()["tools"][0]
    assert tool["validated"] is True
    assert tool["trusted_for_current_run"] is True
    assert tool["last_used_step"] == 2


def test_proof_state_is_receipt_backed_and_surfaces_untrusted_helper_use(tmp_path: Path) -> None:
    store = QueryableReceiptStore(root=tmp_path, run_id="proof")
    store.append(
        "tool_result",
        1,
        "ran helper",
        {"tool_name": "run_command", "arguments": {"cmd": "python helper.py"}, "exit_code": 0},
    )
    registry = TaskLocalToolRegistry(root=tmp_path)
    registry.observe_tool_invocation(
        step=1,
        tool_name="write_file",
        arguments={"path": ".aether/tools/helper.py"},
        exit_code=0,
        evidence_id="evt_00001",
        files_changed=[".aether/tools/helper.py"],
    )
    ledger = {
        "requirements": [
            {
                "requirement": "Produce the final artifact.",
                "status": "proven",
                "evidence_strength": "strong",
                "evidence_refs": ["evt_00001"],
                "evidence_provenance": ["model_authored_artifact"],
                "verifier_blockers": [],
                "next_required_evidence": [],
            }
        ]
    }

    proof_state = _build_proof_state(
        ledger,
        receipt_store=store,
        local_tools=registry,
        completion_policy=make_harness_run_config(system_prompt="s", active_tool_schemas=[]).completion,
    )

    assert proof_state["state"] == "not_ready"
    assert proof_state["rejected_proxy_evidence"]
    assert "helper.py" in json.dumps(proof_state["untrusted_local_tools"])
    assert any("weak or self-authored evidence" in item for item in proof_state["rejected_proxy_evidence"])


def test_inspect_artifact_text_and_missing_file(tmp_path: Path) -> None:
    ctx, workspace = _ctx(tmp_path)
    store = QueryableReceiptStore(root=tmp_path, run_id="t")
    ctx.receipt_store = store
    (workspace / "note.txt").write_text("alpha beta", encoding="utf-8")

    ok = dispatch("inspect_artifact", {"path": "note.txt"}, ctx)
    assert ok.exit_code == 0
    assert "alpha beta" in ok.stdout_head
    assert store.query("note.txt", event_type="artifact_observation")
    missing = dispatch("inspect_artifact", {"path": "missing.txt"}, ctx)
    assert missing.exit_code != 0
    assert "file_not_found" in missing.stderr_head or "missing.txt" in missing.stderr_head


def test_verifier_policy_rounds_are_split_and_clamped() -> None:
    cfg = make_harness_run_config(
        system_prompt="s",
        active_tool_schemas=[],
        verifier_max_rounds=2,
        verifier_immediate_feedback_rounds=9,
        verifier_final_rounds=0,
    )

    assert cfg.verifier.immediate_feedback_rounds == 3
    assert cfg.verifier.final_rounds == 1


def test_failure_card_classifies_refusal_and_service_monitoring() -> None:
    refusal = classify_failure(
        {"mean": 0.0},
        {"summary": "I can't help with that unsafe XSS bypass because of policy."},
        [],
    )
    assert refusal == "PROVIDER_POLICY_REFUSAL"

    card = build_failure_card(
        {
            "row": {"task": "qemu-startup", "condition": "receipt", "mean": 0.0, "steps": 3, "model_calls": 4},
            "status": {"summary": "Connection closed by foreign host.", "verifier_readiness": False},
            "receipt_events": [{"event_type": "tool_result", "summary": "Connection closed by foreign host.", "payload": {}}],
            "model_exchanges": [{"call_role": "normal", "system_prompt_digest": "abc"}],
        }
    )
    assert card["primary_failure_class"] == "SERVICE_MONITORING"
    assert card["recommended_responsible_layer"] == "service lifecycle / candidate preservation"


def test_candidate_preservation_locks_on_listen_ok_and_blocks_ctrl_c(tmp_path: Path) -> None:
    ctx, _workspace = _ctx(tmp_path)
    store = QueryableReceiptStore(root=tmp_path, run_id="t")
    preservation = CandidatePreservation(receipt_store=store)
    ctx.candidate_preservation = preservation

    start = ctx.observe_synthetic(
        {
            "tool": "session_start",
            "exit_code": 0,
            "duration_sec": 0.01,
            "cwd": str(ctx.workspace_root),
            "stdout": "started session svc",
            "stderr": "",
        }
    )
    preservation.observe_invocation(
        type("Record", (), {"step": 1, "tool_name": "session_start", "arguments": {"session_id": "svc", "command": "python -m http.server 6665 --bind 127.0.0.1"}, "envelope": start})()
    )
    probe = ctx.observe_synthetic(
        {
            "tool": "run_command",
            "exit_code": 0,
            "duration_sec": 0.01,
            "cwd": str(ctx.workspace_root),
            "stdout": "LISTEN_OK 127.0.0.1:6665",
            "stderr": "",
        }
    )
    preservation.observe_invocation(
        type("Record", (), {"step": 2, "tool_name": "run_command", "arguments": {"cmd": "probe 127.0.0.1:6665"}, "envelope": probe})()
    )

    assert preservation.active_candidates()[0]["locked"] is True
    blocked = ctx.session_send("svc", "\x03")
    assert blocked.exit_code == 126
    assert "protected candidate" in blocked.stderr_head
    assert "Use non-destructive probes" in blocked.stderr_head
    assert store.query("candidate_viable_locked", event_type="candidate_event")
    assert store.query("candidate_destructive_input_blocked", event_type="candidate_event")


def test_candidate_preservation_locks_on_plain_connected_probe(tmp_path: Path) -> None:
    ctx, _workspace = _ctx(tmp_path)
    store = QueryableReceiptStore(root=tmp_path, run_id="t")
    preservation = CandidatePreservation(receipt_store=store)
    ctx.candidate_preservation = preservation

    start = ctx.observe_synthetic(
        {
            "tool": "session_start",
            "exit_code": 0,
            "duration_sec": 0.01,
            "cwd": str(ctx.workspace_root),
            "stdout": "started session svc",
            "stderr": "",
        }
    )
    preservation.observe_invocation(
        type("Record", (), {"step": 1, "tool_name": "session_start", "arguments": {"session_id": "svc", "command": "python -m http.server 6665 --bind 127.0.0.1"}, "envelope": start})()
    )
    probe = ctx.observe_synthetic(
        {
            "tool": "run_command",
            "exit_code": 0,
            "duration_sec": 0.01,
            "cwd": str(ctx.workspace_root),
            "stdout": "connected\n\x01\x03\x00\x00\n",
            "stderr": "",
        }
    )
    preservation.observe_invocation(
        type("Record", (), {"step": 2, "tool_name": "run_command", "arguments": {"cmd": "probe 127.0.0.1:6665"}, "envelope": probe})()
    )

    assert preservation.active_candidates()[0]["locked"] is True
    blocked = ctx.session_send("svc", "\x03")
    assert blocked.exit_code == 126
    assert store.query("candidate_destructive_input_blocked", event_type="candidate_event")


def test_candidate_preservation_locks_detached_job_not_probe_client(tmp_path: Path) -> None:
    ctx, _workspace = _ctx(tmp_path)
    store = QueryableReceiptStore(root=tmp_path, run_id="t")
    preservation = CandidatePreservation(receipt_store=store)
    ctx.candidate_preservation = preservation

    job = ctx.observe_synthetic(
        {
            "tool": "start_job",
            "exit_code": 0,
            "duration_sec": 0.01,
            "cwd": str(ctx.workspace_root),
            "stdout": "started job svc (pid 123)",
            "stderr": "",
        }
    )
    preservation.observe_invocation(
        type(
            "Record",
            (),
            {
                "step": 1,
                "tool_name": "start_job",
                "arguments": {"job_id": "svc", "cmd": "python -m http.server 6665 --bind 127.0.0.1"},
                "envelope": job,
            },
        )()
    )
    probe_session = ctx.observe_synthetic(
        {
            "tool": "session_start",
            "exit_code": 0,
            "duration_sec": 0.01,
            "cwd": str(ctx.workspace_root),
            "stdout": "started session telnet6665",
            "stderr": "",
        }
    )
    preservation.observe_invocation(
        type(
            "Record",
            (),
            {
                "step": 2,
                "tool_name": "session_start",
                "arguments": {"session_id": "telnet6665", "command": "telnet 127.0.0.1 6665"},
                "envelope": probe_session,
            },
        )()
    )
    probe_read = ctx.observe_synthetic(
        {
            "tool": "session_read",
            "exit_code": None,
            "duration_sec": 0.01,
            "cwd": str(ctx.workspace_root),
            "stdout": "Trying 127.0.0.1...\nConnected to 127.0.0.1.\n",
            "stderr": "",
        }
    )
    preservation.observe_invocation(
        type("Record", (), {"step": 3, "tool_name": "session_read", "arguments": {"session_id": "telnet6665"}, "envelope": probe_read})()
    )

    active = preservation.active_candidates()
    assert len(active) == 1
    assert active[0]["job_id"] == "svc"
    assert active[0]["session_id"] is None


def test_receipt_context_redacts_run_metadata(tmp_path: Path) -> None:
    store = QueryableReceiptStore(root=tmp_path, run_id="qemu-startup")
    store.append(
        "tool_result",
        1,
        "ran in /tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1",
        {
            "raw_log_path": "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1/model_exchange_1.json",
            "stdout_excerpt": "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1",
        },
    )
    policy = make_harness_run_config(system_prompt="s", active_tool_schemas=[]).context_pack
    context = store.context_view(policy=policy)
    rendered = json.dumps(context, sort_keys=True)

    assert context["run_id"] == "current_run"
    assert "raw_log_path" not in rendered
    assert "harbor-jobs" not in rendered
    assert "qemu-startup" not in rendered
    assert "receipt_driven_full" not in rendered


def test_solver_visible_orientation_redacts_host_run_paths() -> None:
    visible = solver_visible_orientation(
        {
            "cwd": "/app",
            "workspace_root": "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1/agent/tmp/harbor_workspace_mirror",
            "writable_paths": [
                "/app",
                "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1/agent/tmp/harbor_workspace_mirror",
            ],
            "tool_presence": {"python3": "/usr/bin/python3"},
            "grader_boundary": {"official_grader": "hidden"},
        }
    )
    rendered = json.dumps(visible, sort_keys=True)

    assert visible["cwd"] == "/app"
    assert "harbor-jobs" not in rendered
    assert "qemu-startup" not in rendered
    assert "receipt_driven_full" not in rendered
    assert "grader_boundary" not in rendered


def test_tool_result_message_redacts_host_run_paths(tmp_path: Path) -> None:
    envelope = build_envelope(
        {
            "tool": "run_command",
            "exit_code": 0,
            "duration_sec": 0.1,
            "cwd": "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1/agent/tmp/harbor_workspace_mirror",
            "stdout": "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1",
            "stderr": "",
        },
        raw_log_dir=tmp_path,
    )
    message = _envelope_to_message("run_command", "call_1", envelope)

    assert "harbor-jobs" not in message["content"]
    assert "qemu-startup" not in message["content"]
    assert "receipt_driven_full" not in message["content"]
    assert "[host_run_path]" in message["content"]


def test_context_serialization_chokepoint_strips_known_bad_benchmark_metadata() -> None:
    ctx = ContextManager()
    ctx.build_prefix(
        system_prompt="kernel",
        task_instruction="do work",
        orientation={"cwd": "/app", "workspace_root": "/app"},
        tool_schemas=[],
        extra_prefix_messages=[
            {
                "role": "system",
                "content": {
                    "task_id": "qemu-startup",
                    "condition": "receipt_driven_full",
                    "benchmark": "terminal-bench",
                    "suite": "official_tasks",
                    "run_id": "qemu-startup__receipt_driven_full",
                    "log": "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1",
                },
            }
        ],
    )
    ctx.append_turn(
        {
            "role": "tool",
            "content": (
                "task_id=qemu-startup condition=receipt_driven_full "
                "benchmark=terminal-bench suite=official_tasks "
                "run_id=qemu-startup__receipt_driven_full "
                "log=/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1"
            ),
        }
    )
    tail_text = ctx.render_tail(
        {
            "proof_state": {
                "task_id": "qemu-startup",
                "condition": "receipt_driven_full",
                "benchmark": "terminal-bench",
                "last_evidence_ref": (
                    "task_id=qemu-startup condition=receipt_driven_full "
                    "benchmark=terminal-bench suite=official_tasks "
                    "log=/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1"
                ),
            },
            "evidence_refs": [
                {
                    "run_id": "qemu-startup__receipt_driven_full",
                    "raw_log_path": "/tmp/harbor-jobs/run/qemu-startup-receipt_driven_full-rep1/model_exchange_1.json",
                    "ref": (
                        "suite=official_tasks benchmark=terminal-bench "
                        "task_id=qemu-startup condition=receipt_driven_full"
                    ),
                }
            ],
        },
        completion_contract={
            "required_final_evidence": [
                "task_id=qemu-startup benchmark=terminal-bench suite=official_tasks"
            ]
        },
    )
    visible = "\n".join(message["content"] for message in ctx.message_history()) + "\n" + tail_text

    for forbidden in (
        "terminal-bench",
        "official_tasks",
        "receipt_driven_full",
        "qemu-startup__receipt_driven_full",
        "/tmp/harbor-jobs/",
    ):
        assert forbidden not in visible
    assert "[redacted_metadata]" in visible
    assert "[host_run_path]" in visible


def test_prompt_audit_serialization_redacts_dynamic_metadata_everywhere(tmp_path: Path) -> None:
    metadata = {
        "task_id": "novel-task-42",
        "condition": "condition-zeta",
        "benchmark": "fresh-benchmark-123",
        "suite": "suite_alpha_v9",
        "row_id": "row-special-777",
        "run_id": "run-special-4242",
        "output_root": "/tmp/custom-output/run-special-4242",
        "source_path": "/var/tmp/source/tree/row-special-777/task.json",
    }
    store = QueryableReceiptStore(root=tmp_path, run_id=metadata["run_id"])
    store.append(
        "tool_result",
        1,
        (
            "inspected task_id=novel-task-42 benchmark=fresh-benchmark-123 "
            "suite=suite_alpha_v9 row_id=row-special-777"
        ),
        {
            **metadata,
            "stdout_excerpt": (
                "condition=condition-zeta output_root=/tmp/custom-output/run-special-4242 "
                "source_path=/var/tmp/source/tree/row-special-777/task.json"
            ),
            "ref": "task_id=novel-task-42 run_id=run-special-4242",
            "raw_log_path": "/tmp/custom-output/run-special-4242/model_exchange_1.json",
        },
    )
    policy = ContextPackPolicy(
        include_sections=("success_contract", "current_plan", "recent_steps", "evidence_refs"),
        always_include=("success_contract", "current_plan"),
    )
    receipt_context = store.context_view(policy=policy)

    ctx = ContextManager()
    ctx.build_prefix(
        system_prompt="kernel",
        task_instruction="do work",
        orientation={"cwd": "/app", **metadata},
        tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": "inspect_artifact",
                    "description": (
                        "Use source_path=/var/tmp/source/tree/row-special-777/task.json "
                        "for row_id=row-special-777"
                    ),
                },
            }
        ],
        extra_prefix_messages=[
            {"role": "system", "content": metadata},
            {"role": "system", "content": {"receipt_context": receipt_context, **metadata}},
        ],
    )
    ctx.append_turn(
        {
            "role": "tool",
            "content": (
                "task_id=novel-task-42 condition=condition-zeta benchmark=fresh-benchmark-123 "
                "suite=suite_alpha_v9 row_id=row-special-777 run_id=run-special-4242 "
                "output_root=/tmp/custom-output/run-special-4242"
            ),
        }
    )
    tail_text = ctx.render_tail(
        {
            "proof_state": {
                **metadata,
                "next_required_evidence": (
                    "Inspect source_path=/var/tmp/source/tree/row-special-777/task.json "
                    "for task_id=novel-task-42"
                ),
            },
            "evidence_refs": [
                {
                    **metadata,
                    "ref": "benchmark=fresh-benchmark-123 suite=suite_alpha_v9",
                }
            ],
            "receipt_context": receipt_context,
        },
        completion_contract={
            "required_final_evidence": [
                (
                    "task_id=novel-task-42 benchmark=fresh-benchmark-123 "
                    "condition=condition-zeta row_id=row-special-777"
                )
            ]
        },
    )
    full_payload = {
        "prefix": ctx.message_history(),
        "tail": ctx.current_tail_payload(),
        "receipt_context": receipt_context,
        "tail_text": tail_text,
    }
    raw_receipt_text = json.dumps(receipt_context, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    visible = json.dumps(
        sanitize_model_visible_payload(full_payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    for forbidden in metadata.values():
        assert forbidden not in raw_receipt_text
        assert forbidden not in visible
    assert "/tmp/custom-output" not in visible
    assert "/var/tmp/source/tree" not in visible
    assert "[redacted_metadata]" in visible


def test_context_serialization_redacts_literal_metadata_values_across_prompt_surfaces(tmp_path: Path) -> None:
    task_id = "aurora-needle-17"
    run_id = f"{task_id}__receipt_probe"
    condition = "phase_0_5"
    suite = "suite_lantern"
    benchmark = "benchmark_lantern"
    row_id = "row_017"
    output_root = str(tmp_path / "output" / task_id)
    source_path = str(tmp_path / "source" / f"{task_id}.json")

    ctx = ContextManager()
    ctx.build_prefix(
        system_prompt="kernel",
        task_instruction=(
            "task_id={task_id} run_id={run_id} condition={condition} suite={suite} "
            "benchmark={benchmark} row_id={row_id} output_root={output_root} source_path={source_path}"
        ).format(
            task_id=task_id,
            run_id=run_id,
            condition=condition,
            suite=suite,
            benchmark=benchmark,
            row_id=row_id,
            output_root=output_root,
            source_path=source_path,
        ),
        orientation={"cwd": "/app", "workspace_root": "/app"},
        tool_schemas=[],
        extra_prefix_messages=[
            {
                "role": "system",
                "content": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "condition": condition,
                    "suite": suite,
                    "benchmark": benchmark,
                    "row_id": row_id,
                    "output_root": output_root,
                    "source_path": source_path,
                    "proof_state": {
                        "task_id": task_id,
                        "run_id": run_id,
                        "condition": condition,
                        "suite": suite,
                        "benchmark": benchmark,
                        "row_id": row_id,
                    },
                    "evidence_refs": [
                        {
                            "task_id": task_id,
                            "run_id": run_id,
                            "condition": condition,
                            "suite": suite,
                            "benchmark": benchmark,
                            "row_id": row_id,
                            "output_root": output_root,
                            "source_path": source_path,
                        }
                    ],
                },
            }
        ],
    )
    ctx.append_turn(
        {
            "role": "assistant",
            "content": (
                "task_id={task_id} run_id={run_id} condition={condition} suite={suite} "
                "benchmark={benchmark} row_id={row_id} output_root={output_root} source_path={source_path}"
            ).format(
                task_id=task_id,
                run_id=run_id,
                condition=condition,
                suite=suite,
                benchmark=benchmark,
                row_id=row_id,
                output_root=output_root,
                source_path=source_path,
            ),
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "inspect_artifact",
                        "arguments": json.dumps(
                            {
                                "task_id": task_id,
                                "run_id": run_id,
                                "condition": condition,
                                "suite": suite,
                                "benchmark": benchmark,
                                "row_id": row_id,
                                "output_root": output_root,
                                "source_path": source_path,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ),
                    },
                }
            ],
        }
    )
    tail_text = ctx.render_tail(
        {
            "task_id": task_id,
            "run_id": run_id,
            "condition": condition,
            "suite": suite,
            "benchmark": benchmark,
            "row_id": row_id,
            "output_root": output_root,
            "source_path": source_path,
            "proof_state": {
                "task_id": task_id,
                "run_id": run_id,
                "condition": condition,
                "suite": suite,
                "benchmark": benchmark,
                "row_id": row_id,
            },
            "evidence_refs": [
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "condition": condition,
                    "suite": suite,
                    "benchmark": benchmark,
                    "row_id": row_id,
                    "output_root": output_root,
                    "source_path": source_path,
                }
            ],
        },
        completion_contract={
            "required_final_evidence": [
                (
                    "task_id={task_id} run_id={run_id} condition={condition} suite={suite} "
                    "benchmark={benchmark} row_id={row_id} output_root={output_root} source_path={source_path}"
                ).format(
                    task_id=task_id,
                    run_id=run_id,
                    condition=condition,
                    suite=suite,
                    benchmark=benchmark,
                    row_id=row_id,
                    output_root=output_root,
                    source_path=source_path,
                )
            ]
        },
    )

    store = QueryableReceiptStore(root=tmp_path, run_id=run_id)
    store.append(
        "tool_result",
        3,
        (
            "task_id={task_id} run_id={run_id} condition={condition} suite={suite} "
            "benchmark={benchmark} row_id={row_id}"
        ).format(
            task_id=task_id,
            run_id=run_id,
            condition=condition,
            suite=suite,
            benchmark=benchmark,
            row_id=row_id,
        ),
        {
            "task_id": task_id,
            "run_id": run_id,
            "condition": condition,
            "suite": suite,
            "benchmark": benchmark,
            "row_id": row_id,
            "output_root": output_root,
            "source_path": source_path,
            "raw_log_path": str(tmp_path / "logs" / run_id / "model_exchange_3.json"),
            "proof_state": {
                "task_id": task_id,
                "run_id": run_id,
                "condition": condition,
                "suite": suite,
                "benchmark": benchmark,
                "row_id": row_id,
            },
            "evidence_refs": [
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "condition": condition,
                    "suite": suite,
                    "benchmark": benchmark,
                    "row_id": row_id,
                    "output_root": output_root,
                    "source_path": source_path,
                }
            ],
        },
    )
    receipt_context = store.context_view(
        policy=ContextPackPolicy(
            include_sections=("success_contract", "current_plan", "recent_steps", "evidence_refs"),
            always_include=("success_contract", "current_plan"),
        )
    )

    prefix_text = json.dumps(ctx.message_history(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    receipt_text = json.dumps(receipt_context, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    prompt_payload = {
        "prefix": ctx.message_history(),
        "tail": tail_text,
        "receipt_context": receipt_context,
    }
    rendered = json.dumps(
        sanitize_model_visible_payload(prompt_payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    assert task_id not in prefix_text
    assert run_id not in prefix_text
    assert condition not in prefix_text
    assert suite not in prefix_text
    assert benchmark not in prefix_text
    assert row_id not in prefix_text
    assert output_root not in prefix_text
    assert source_path not in prefix_text

    assert task_id not in tail_text
    assert run_id not in tail_text
    assert condition not in tail_text
    assert suite not in tail_text
    assert benchmark not in tail_text
    assert row_id not in tail_text
    assert output_root not in tail_text
    assert source_path not in tail_text

    assert task_id not in receipt_text
    assert run_id not in receipt_text
    assert condition not in receipt_text
    assert suite not in receipt_text
    assert benchmark not in receipt_text
    assert row_id not in receipt_text
    assert output_root not in receipt_text
    assert source_path not in receipt_text
    assert "[redacted_metadata]" in receipt_text
    assert "current_run" in receipt_text

    for literal in (task_id, run_id, condition, suite, benchmark, row_id, output_root, source_path):
        assert literal not in rendered
    assert "[redacted_metadata]" in rendered


def test_candidate_events_appear_in_receipt_context(tmp_path: Path) -> None:
    store = QueryableReceiptStore(root=tmp_path, run_id="t")
    store.append(
        "candidate_event",
        2,
        "candidate viable",
        {"candidate_event_type": "candidate_viable_locked", "candidate": {"session_id": "svc", "locked": True}},
    )
    policy = make_harness_run_config(system_prompt="s", active_tool_schemas=[]).context_pack
    context = store.context_view(policy=policy)

    assert "active_candidates" in context
    assert context["active_candidates"][0]["payload"]["candidate"]["session_id"] == "svc"
