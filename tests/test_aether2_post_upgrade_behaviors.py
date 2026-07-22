from __future__ import annotations

import json
import time
from pathlib import Path

from harness.aether2.control.loop import run_aether2_loop
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.model_client import ModelResponse
from harness.aether2.runtime.task_spec import TaskSpec


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


def _make_task(tmp_path: Path, instruction: str = "write the file") -> TaskSpec:
    task_dir = tmp_path / "task"
    workspace_root = task_dir / "workspace"
    artifacts_dir = task_dir / "artifacts"
    workspace_root.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)
    return TaskSpec(
        task_id="post-upgrade-task",
        instruction=instruction,
        task_dir=task_dir,
        workspace_root=workspace_root,
        artifacts_dir=artifacts_dir,
    )


class _RecordingClient:
    def __init__(self, turns: list[ModelResponse]) -> None:
        self.turns = list(turns)
        self.normal_messages: list[str] = []

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        del tools, cache_prefix_len
        joined = "\n".join(str(message.get("content", "")) for message in messages)
        if "fresh-context verifier" in joined:
            payload = {
                "requirements": [
                    {
                        "requirement": "task complete",
                        "verdict": "unverifiable",
                        "evidence": "Need stronger evidence.",
                        "unresolved": True,
                    }
                ],
                "reason_codes": ["need_stronger_evidence"],
                "summary": "Need stronger evidence.",
            }
            return _response(text=json.dumps(payload))
        self.normal_messages.append(joined)
        if self.turns:
            return self.turns.pop(0)
        return _response(text="done")


def test_receipt_variant_records_task_operating_contract(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="write hello.txt with hello")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _RecordingClient(
        [
            _response(
                text=(
                    "[TASK_OPERATING_CONTRACT]\n"
                    '{"required_final_state":["hello.txt contains hello"],'
                    '"proof_that_counts":["read back hello.txt"],'
                    '"proxy_evidence_that_does_not_count":["file exists"],'
                    '"irreversible_or_bulk_actions":["overwrite hello.txt"],'
                    '"real_effect_to_observe":["hello.txt content"],'
                    '"environment_or_tool_discovery":["confirm workspace root"],'
                    '"first_evidence_plan":["write hello.txt then read it"]}'
                ),
                tool_calls=(
                    _tool_call("write_file", {"path": "hello.txt", "content": "hello"}),
                    _tool_call("read_file", {"path": "hello.txt"}),
                    _tool_call("task_done", {"summary": "done", "checks": ["python3 - <<'PY'\nfrom pathlib import Path\nassert Path('hello.txt').read_text() == 'hello'\nPY"]}),
                ),
            ),
            _response(text="blocked"),
        ]
    )

    run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 30,
        receipt_driven_variant_enabled=True,
    )

    operating_contract_path = task.workspace_root / ".aether2" / "receipt_store" / "task_operating_contract.json"
    stored = json.loads(operating_contract_path.read_text(encoding="utf-8"))
    assert stored["required_final_state"] == ["hello.txt contains hello"]
    assert "task_operating_contract" in (task.workspace_root / ".aether2" / "receipt_store" / "events.jsonl").read_text(encoding="utf-8")


def test_premature_task_done_emits_warning_before_execution(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="write out.txt")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _RecordingClient(
        [
            _response(
                text=(
                    "[TASK_OPERATING_CONTRACT]\n"
                    '{"required_final_state":["out.txt exists"],"proof_that_counts":["read back out.txt"],'
                    '"proxy_evidence_that_does_not_count":["claiming done"],"irreversible_or_bulk_actions":[],'
                    '"real_effect_to_observe":["out.txt present"],"environment_or_tool_discovery":["inspect workspace"],'
                    '"first_evidence_plan":["write out.txt"]}'
                ),
                tool_calls=(_tool_call("task_done", {"summary": "done", "checks": []}),),
            ),
            _response(text="blocked after warning"),
        ]
    )

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 30,
        receipt_driven_variant_enabled=True,
    )

    assert result.finalize_reason in {"implicit_stop", "task_blocked"}
    assert any("task_done_warning" in message for message in client.normal_messages[1:])
    assert all(record.tool_name != "task_done" for record in result.tool_invocations)


def test_cost_budget_kill_switch_stops_run_before_next_call(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="keep working")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    expensive = ModelResponse(
        text="still working",
        tool_calls=(_tool_call("write_file", {"path": "note.txt", "content": "x"}),),
        usage={"cached_input_tokens": 0, "fresh_input_tokens": 0, "output_tokens": 1_000_000},
        status="completed",
        raw_response={},
    )
    # Plenty of turns available; the spend ceiling — not turn exhaustion — must stop it.
    client = _RecordingClient([expensive for _ in range(20)])

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 30,
        cost_budget_usd=1.0,
        cost_input_per_mtok=30.0,
        cost_output_per_mtok=180.0,
    )

    # One expensive call = 1e6 output tokens * $180/Mtok = $180, far over the $1 ceiling.
    assert result.finalize_reason == "cost_budget_exhausted"
    assert result.cost_estimate >= 1.0
    # Stopped on the spend ceiling, not by exhausting the 20 available turns.
    assert 1 <= result.model_calls < 20


def test_estimate_token_cost_applies_long_context_surcharge() -> None:
    from harness.aether2.control.reasoning_trace import _estimate_token_cost

    under = {"fresh_input_tokens": 200_000, "cached_input_tokens": 0, "output_tokens": 0}
    over = {"fresh_input_tokens": 300_000, "cached_input_tokens": 0, "output_tokens": 0}
    rate = dict(input_per_mtok=30.0, output_per_mtok=180.0, cached_input_discount=0.1)

    # 200K input * $30/Mtok = $6.00, no surcharge.
    assert abs(_estimate_token_cost(under, **rate) - 6.0) < 1e-6
    # 300K input is above the 272K threshold -> 2x input rate -> 300000 * 60 / 1e6 = $18.00.
    assert abs(_estimate_token_cost(over, **rate) - 18.0) < 1e-6


def test_cost_budget_disabled_by_default_does_not_stop_run(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="write hello.txt with hello")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _RecordingClient(
        [
            ModelResponse(
                text="done",
                tool_calls=(_tool_call("write_file", {"path": "hello.txt", "content": "hello"}),),
                usage={"cached_input_tokens": 0, "fresh_input_tokens": 0, "output_tokens": 5_000_000},
                status="completed",
                raw_response={},
            ),
            _response(text="finished"),
        ]
    )

    # No budget passed: huge usage must not trigger a cost stop (baseline preserved).
    result = run_aether2_loop(task, client, executor, deadline_ts=time.monotonic() + 30)
    assert result.finalize_reason != "cost_budget_exhausted"


def test_repeat_progress_note_and_mutation_warning_surface_to_model(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="fix the workspace safely")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    (task.workspace_root / "a.txt").write_text("a", encoding="utf-8")
    (task.workspace_root / "b.txt").write_text("b", encoding="utf-8")
    client = _RecordingClient(
        [
            _response(
                text=(
                    "[TASK_OPERATING_CONTRACT]\n"
                    '{"required_final_state":["inspect workspace safely"],"proof_that_counts":["read file contents"],'
                    '"proxy_evidence_that_does_not_count":["bulk move"],"irreversible_or_bulk_actions":["mv *.txt archive/"],'
                    '"real_effect_to_observe":["file contents inspected"],"environment_or_tool_discovery":["list files"],'
                    '"first_evidence_plan":["read files before mutating"]}'
                ),
                tool_calls=(_tool_call("run_command", {"cmd": "mv *.txt archive/"}),),
            ),
            _response(text="retry one", tool_calls=(_tool_call("run_command", {"cmd": "python3 missing.py"}),)),
            _response(text="retry two", tool_calls=(_tool_call("run_command", {"cmd": "python3 missing.py"}),)),
            _response(text="stop"),
        ]
    )

    run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 30,
        receipt_driven_variant_enabled=True,
    )

    assert any("mutation_warning" in message for message in client.normal_messages[1:])
    assert any("Progress note: you tried a similar action last turn." in message for message in client.normal_messages[3:])


def test_interactive_start_job_failure_emits_recovery_warning(tmp_path: Path) -> None:
    task = _make_task(tmp_path, instruction="launch the interactive tool correctly")
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _RecordingClient(
        [
            _response(
                text=(
                    "[TASK_OPERATING_CONTRACT]\n"
                    '{"required_final_state":["interactive process launched correctly"],'
                    '"proof_that_counts":["observe attached interactive output"],'
                    '"proxy_evidence_that_does_not_count":["retrying detached launches"],'
                    '"irreversible_or_bulk_actions":[],'
                    '"real_effect_to_observe":["interactive output visible"],'
                    '"environment_or_tool_discovery":["check tool contract"],'
                    '"first_evidence_plan":["launch the process"]}'
                ),
                tool_calls=(
                    _tool_call(
                        "start_job",
                        {"cmd": "qemu-system-x86_64 -nographic -serial mon:stdio -drive file=disk.qcow2", "job_id": "interactive"},
                    ),
                ),
            ),
            _response(text="stop after warning"),
        ]
    )

    run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 30,
        receipt_driven_variant_enabled=True,
    )

    assert any("interactive_tool_recovery" in message for message in client.normal_messages[1:])
    assert any("session_start" in message for message in client.normal_messages[1:])
