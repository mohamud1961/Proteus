from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aether_next_build"))

from aether_next.kernel import KernelResult
from aether_next.execution import CommandResult
from aether_next.kernel import AetherNextKernel
from aether_next.ledger import Receipt
from aether_next.no_progress import NoProgressController
from aether_next.runtime_ir import ActionRequest
from aether_next.real_executor import SubprocessExecutor
from aether_next.runners import docker_runner
from aether_next.ledger import ExecutionLedger
from aether_next.verifier import parse_model_verifier_result
from aether_next.verifier_probes import inspect_artifact_probe


def test_timeout_that_grader_passes_is_not_classified_as_task_failure() -> None:
    classification = SimpleNamespace(
        label="timeout_resource_failure",
        confidence="high",
        detail="agent phase timed out",
    )
    result = KernelResult(
        status="timeout",
        step=34,
        reconfigurations=0,
        blockers=("kernel_timeout_after_1800s",),
    )

    record_status, label, confidence, detail = docker_runner._classification_fields_for_record(
        classification=classification,
        result=result,
        reward=1.0,
        grader_error=None,
        kernel_timed_out=True,
    )

    assert record_status == "timeout"
    assert label == "none"
    assert confidence == "high"
    assert "grader passed" in detail
    assert "inefficient" in detail


def test_timeout_that_grader_fails_keeps_timeout_resource_label() -> None:
    classification = SimpleNamespace(
        label="timeout_resource_failure",
        confidence="high",
        detail="agent phase timed out",
    )
    result = KernelResult(
        status="timeout",
        step=49,
        reconfigurations=0,
        blockers=("kernel_timeout_after_1800s",),
    )

    record_status, label, confidence, detail = docker_runner._classification_fields_for_record(
        classification=classification,
        result=result,
        reward=0.0,
        grader_error=None,
        kernel_timed_out=True,
    )

    assert record_status == "timeout"
    assert label == "timeout_resource_failure"
    assert confidence == "high"
    assert detail == "agent phase timed out"


def test_official_yaml_grader_uses_exit_code_when_reward_file_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_dir = tmp_path / "official-yaml"
    task_dir.mkdir()
    (task_dir / "run-tests.sh").write_text("#!/bin/bash\npytest\n", encoding="utf-8")

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "cat: /logs/verifier/reward.txt: No such file"

    monkeypatch.setattr(docker_runner.subprocess, "run", lambda *_args, **_kwargs: Proc())

    reward, error, source = docker_runner._resolve_grader_reward(
        container_id="container",
        task_dir=str(task_dir),
        grader_exit=0,
        grader_error=None,
    )

    assert reward == 1.0
    assert error is None
    assert source == "official_run_tests_exit_code"


def test_mirrored_layout_still_requires_reward_file(tmp_path: Path, monkeypatch) -> None:
    task_dir = tmp_path / "mirrored"
    task_dir.mkdir()

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "cat: /logs/verifier/reward.txt: No such file"

    monkeypatch.setattr(docker_runner.subprocess, "run", lambda *_args, **_kwargs: Proc())

    reward, error, source = docker_runner._resolve_grader_reward(
        container_id="container",
        task_dir=str(task_dir),
        grader_exit=0,
        grader_error=None,
    )

    assert reward == 0.0
    assert error == "reward.txt missing or empty"
    assert source == "reward_txt_missing"


def test_docker_probe_service_host_port_uses_tcp_probe(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        del kwargs
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="open\n", stderr="")

    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)
    executor = docker_runner.DockerExecExecutor("container123", str(tmp_path))

    result = executor.probe_process("localhost:5328")

    assert result.live is True
    assert result.detail == "open"
    assert calls
    assert calls[0][:4] == ["docker", "exec", "container123", "python3"]
    assert "pgrep" not in calls[0]


def test_docker_probe_service_plain_name_uses_process_probe(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        del kwargs
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="123 python3 server.py\n", stderr="")

    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)
    executor = docker_runner.DockerExecExecutor("container123", str(tmp_path))

    result = executor.probe_process("kvstore-server")

    assert result.live is True
    assert result.detail == "123 python3 server.py"
    assert calls
    assert calls[0] == ["docker", "exec", "container123", "pgrep", "-f", "kvstore-server"]


def test_binary_preview_reports_metadata_only_not_semantic_success(tmp_path: Path) -> None:
    (tmp_path / "code.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    executor = SubprocessExecutor(str(tmp_path))

    result = executor.inspect_artifact("code.png", "preview")

    assert result.success is False
    assert result.extracted_text == ""
    assert result.metadata["semantic_content_available"] is False
    assert "semantic content unavailable" in result.detail


def test_binary_mode_can_return_metadata_without_claiming_semantics(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    executor = SubprocessExecutor(str(tmp_path))

    result = executor.inspect_artifact("blob.bin", "binary")

    assert result.success is True
    assert result.extracted_text == ""
    assert result.metadata["semantic_content_available"] is False
    assert "binary metadata" in result.detail


def test_verifier_image_artifact_probe_reports_metadata_only() -> None:
    class FakeExecutor:
        def run_command(self, command: str, *, timeout_s: int = 30) -> CommandResult:
            del timeout_s
            if "test -e" in command:
                return CommandResult(command, 0, stdout="yes\n")
            if "file -b" in command:
                return CommandResult(command, 0, stdout="PNG image data\n")
            if "stat -c %s" in command:
                return CommandResult(command, 0, stdout="95\n")
            if "stat -c '%a %U %Y'" in command:
                return CommandResult(command, 0, stdout="644 root 123\n")
            if "sha256sum" in command:
                return CommandResult(command, 0, stdout=("a" * 64) + "  code.png\n")
            if "identify" in command:
                return CommandResult(command, 0, stdout="code.png PNG 1160x804\n")
            return CommandResult(command, 0, stdout="")

    row = inspect_artifact_probe(FakeExecutor(), "code.png")

    assert row["exists"] is True
    assert row["semantic_content_available"] is False
    assert row["semantic_content_status"].startswith("metadata_only")


def test_uncertain_missing_evidence_becomes_blocking_findings() -> None:
    result = parse_model_verifier_result({
        "verdict": "uncertain_missing_evidence",
        "confidence": "high",
        "summary": "Need raw state.",
        "missing_evidence_requests": [
            "Provide a transcript showing Ctrl-C reaches the foreground process.",
            "Provide a read-only excerpt of headless_terminal.py.",
        ],
    })

    assert result.verdict == "uncertain_missing_evidence"
    assert len(result.findings) == 2
    assert all(finding.priority == "blocking" for finding in result.findings)
    assert all(finding.applies_to[0] == "completion_evidence" for finding in result.findings)
    assert all(finding.applies_to[1].startswith("missing_request:") for finding in result.findings)
    assert "Ctrl-C" in result.findings[0].summary


def test_missing_evidence_findings_are_active_in_ledger() -> None:
    result = parse_model_verifier_result({
        "verdict": "uncertain_missing_evidence",
        "confidence": "high",
        "summary": "Need raw state.",
        "missing_evidence_requests": [
            "Provide a live service round-trip transcript.",
        ],
    })
    ledger = ExecutionLedger()

    ledger.apply_verifier_result(result, step=12)

    active = ledger.active_finding_context(13)
    assert len(active) == 1
    assert active[0]["priority"] == "blocking"
    assert "service round-trip" in active[0]["summary"]


def test_multiple_missing_evidence_requests_remain_active_in_ledger() -> None:
    result = parse_model_verifier_result({
        "verdict": "uncertain_missing_evidence",
        "confidence": "high",
        "summary": "Need two independent observations.",
        "missing_evidence_requests": [
            "Provide a raw input excerpt.",
            "Provide a verifier-owned rerun of the output check.",
        ],
    })
    ledger = ExecutionLedger()

    ledger.apply_verifier_result(result, step=12)

    active = ledger.active_finding_context(13)
    assert len(active) == 2
    assert {item["summary"] for item in active} == {
        "Missing inspectable completion evidence: Provide a raw input excerpt.",
        "Missing inspectable completion evidence: Provide a verifier-owned rerun of the output check.",
    }
    assert all("missing_request:" in item["applies_to"][1] for item in active)


def test_active_missing_evidence_requires_intervening_evidence_before_resubmit() -> None:
    result = parse_model_verifier_result({
        "verdict": "uncertain_missing_evidence",
        "confidence": "high",
        "summary": "Need raw state.",
        "missing_evidence_requests": ["Provide a raw transcript."],
    })
    ledger = ExecutionLedger()
    ledger.apply_verifier_result(result, step=7)

    assert AetherNextKernel._active_findings_need_intervening_evidence(ledger) is True

    ledger.record(Receipt(
        receipt_id="step-8:read-transcript",
        step=8,
        kind="read_file",
        success=True,
        summary="read transcript",
    ))

    assert AetherNextKernel._active_findings_need_intervening_evidence(ledger) is False


def test_no_progress_detects_repeated_artifact_inspection() -> None:
    ledger = ExecutionLedger()
    for step in range(2):
        ledger.record(Receipt(
            receipt_id=f"step-{step}:inspect",
            step=step,
            kind="artifact_inspection",
            success=False,
            summary="metadata-only inspection",
            failure_class="perception_required",
            payload={"path": "code.png", "mode": "preview"},
        ))
    action = ActionRequest(
        action_id="inspect-again",
        kind="inspect_artifact",
        capability_id="artifact_inspection",
        arguments={"path": "/app/code.png", "mode": "preview"},
        intent="inspect the image again",
        expected_observation="semantic contents of the image",
        if_fail_next="use another extraction path",
    )

    decision = NoProgressController().evaluate(action, ledger)

    assert decision is not None
    assert decision.reason_code == "repeated_artifact_inspection_no_state_change"
    assert decision.action_family == "artifact_inspection"


def test_no_progress_detects_repeated_failed_service_probe() -> None:
    ledger = ExecutionLedger()
    for step in range(2):
        ledger.record(Receipt(
            receipt_id=f"step-{step}:probe",
            step=step,
            kind="service_probe",
            success=False,
            summary="probe localhost:5328: not_live",
            failure_class="service_not_ready",
            payload={"target": "localhost:5328"},
        ))
    action = ActionRequest(
        action_id="probe-again",
        kind="probe_service",
        capability_id="service_probe",
        arguments={"target": "localhost:5328"},
        intent="check whether the service is live again",
        expected_observation="open TCP service",
        if_fail_next="inspect service logs or relaunch",
    )

    decision = NoProgressController().evaluate(action, ledger)

    assert decision is not None
    assert decision.reason_code == "repeated_service_probe_no_state_change"
    assert decision.action_family == "service_probe"


def test_official_yaml_task_metadata_and_instruction_are_public_inputs(tmp_path: Path) -> None:
    from aether_next.task_metadata_loader import load_task_instruction, load_task_metadata

    task_dir = tmp_path / "official-yaml"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        "instruction: |-\n"
        "  Build the requested artifact.\n"
        "  Verify it independently.\n"
        "difficulty: hard\n"
        "category: data-processing\n"
        "tags:\n"
        "  - logs\n"
        "max_agent_timeout_sec: 900.0\n"
        "max_test_timeout_sec: 180.0\n",
        encoding="utf-8",
    )

    metadata = load_task_metadata(task_dir)

    assert load_task_instruction(task_dir) == "Build the requested artifact.\nVerify it independently."
    assert metadata["metadata"]["category"] == "data-processing"
    assert metadata["metadata"]["tags"] == ["logs"]
    assert metadata["agent"]["timeout_sec"] == 900.0
    assert metadata["verifier"]["timeout_sec"] == 180.0
