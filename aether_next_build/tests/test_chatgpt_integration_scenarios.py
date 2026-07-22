from __future__ import annotations

import json
from pathlib import Path

from aether_next.integration_scenarios import (
    run_disabled_tool_guard_scenario,
    run_workbench_verifier_repair_scenario,
)
from run_verifier_only_eval import _case_semantic_wrong, _env as _verifier_env, _model_messages
from aether_next.verifier_packets import build_verifier_packet

_BUILD_ROOT = Path(__file__).resolve().parents[1]


def test_workbench_verifier_repair_loop_exercises_real_kernel_stack() -> None:
    result = run_workbench_verifier_repair_scenario()

    assert result.status == "completed"
    assert result.final_files["out.txt"] == "PASS-123\n"
    assert result.checks == {
        "completed": True,
        "verifier_blocked_first_submit": True,
        "active_finding_reached_context": True,
        "artifact_changed_after_finding": True,
        "final_content_exact": True,
    }
    kinds = [receipt["kind"] for receipt in result.receipts]
    assert "config_realization" in kinds
    assert "query_artifact_history" in kinds
    assert "inspect_diff" in kinds
    assert "record_observation" in kinds
    verifier_verdicts = [
        receipt.get("verdict")
        for receipt in result.receipts
        if receipt["kind"] == "model_verifier_result"
    ]
    assert verifier_verdicts[0] == "needs_repair"
    assert "completed" in verifier_verdicts[1:]

    first_verifier_packet = result.verifier_packets[0]
    second_context_packet = next(
        packet for packet in result.context_packets[2:]
        if "active_completion_findings" in packet
    )
    # The Verifier receives immutable task truth, not the Architect's private
    # inspection strategy or prompt.  The real task contract remains visible.
    assert first_verifier_packet["task_contract"]["raw_task_prompt"] == "Create out.txt containing the exact token PASS-123."
    assert "architect_verifier_prompt" not in json.dumps(first_verifier_packet, sort_keys=True)
    assert "artifact_history" not in first_verifier_packet
    assert "memory_events" not in first_verifier_packet
    assert "solver_authored_evidence" not in first_verifier_packet
    assert second_context_packet["active_completion_findings"]
    assert second_context_packet["context_recipe_realization"]["enabled"] is True


def test_stable_core_tools_prevent_architect_omission_from_hiding_shell() -> None:
    result = run_disabled_tool_guard_scenario()

    assert result.status == "completed"
    assert result.checks["stable_core_shell_visible"] is True
    assert result.checks["mixed_dispatch_allowed_for_core_tools"] is True
    assert result.checks["status_completed_with_stable_core_tools"] is True
    assert result.final_files["out.txt"] == "OK\n"
    assert any(receipt["kind"] == "run_command" for receipt in result.receipts)


def test_verifier_only_model_prompt_is_strict_evidence_bound() -> None:
    item = _case_semantic_wrong()
    packet = build_verifier_packet(
        item["compiled"],
        item["ledger"],
        step=3,
        reason=item["reason"],
        envmap=_verifier_env(item["compiled"].task_prompt),
    )
    messages = _model_messages(packet)

    assert messages[0]["role"] == "system"
    assert "Judge the actual current task state" in messages[0]["content"]
    assert "Official benchmark grading remains external." in messages[1]["content"]
    assert "PASS-124" not in messages[1]["content"]
    assert "state_inspection_handles" in messages[1]["content"]
    assert "solver_authored_evidence" not in messages[1]["content"]


def test_deterministic_integration_runner_writes_auditable_bundle(tmp_path: Path) -> None:
    import subprocess
    import sys

    out_dir = tmp_path / "deterministic_integration"
    proc = subprocess.run(
        [sys.executable, "run_deterministic_integration_eval.py", "--out-dir", str(out_dir)],
        cwd=_BUILD_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    payload = json.loads(proc.stdout)
    out_dir = Path(payload["out_dir"]).resolve()
    assert out_dir.exists()
    assert (out_dir / "summary.json").exists()
    assert (out_dir / "DETERMINISTIC_INTEGRATION_REPORT.md").exists()
    assert {row["scenario_id"] for row in payload["rows"]} == {
        "workbench_verifier_repair_loop",
        "stable_core_tool_guard",
    }


def test_verifier_only_validator_accepts_fake_bundle_and_writes_report(tmp_path: Path) -> None:
    import subprocess
    import sys

    out_dir = tmp_path / "verifier_fake"
    subprocess.run(
        [sys.executable, str(_BUILD_ROOT / "run_verifier_only_eval.py"), "--mode", "fake", "--out-dir", str(out_dir)],
        cwd=_BUILD_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    report = tmp_path / "VALIDATION.md"
    proc = subprocess.run(
        [sys.executable, "validate_verifier_only_eval.py", str(out_dir), "--report", str(report)],
        cwd=_BUILD_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert {case["case"] for case in payload["cases"]} == {
        "semantic_wrong",
        "solver_claim_conflicts_with_raw_state",
        "missing_artifact",
        "schema_mismatch",
        "repeated_no_progress",
        "insufficient_evidence",
    }
    assert report.exists()
    assert "Overall: `PASS`" in report.read_text()


def test_verifier_only_validator_rejects_parse_error_bundle(tmp_path: Path) -> None:
    import subprocess
    import sys

    out_dir = tmp_path / "bad_bundle"
    out_dir.mkdir()
    summary_rows = []
    for case in [
        "semantic_wrong",
        "solver_claim_conflicts_with_raw_state",
        "missing_artifact",
        "schema_mismatch",
        "repeated_no_progress",
        "insufficient_evidence",
    ]:
        case_dir = out_dir / case
        case_dir.mkdir()
        (case_dir / "verifier_packet.json").write_text(json.dumps({
            "task_prompt": "task",
            "success_definition": "success",
            "local_verification_limits": [],
            "artifact_evidence": [],
            "artifact_history": [],
            "memory_events": [],
            "recent_receipts": [],
            "reason": "test",
        }))
        (case_dir / "raw_output.json").write_text("{}")
        (case_dir / "parsed_result.json").write_text(json.dumps({"parse_error": "bad"}))
        (case_dir / "active_findings_after.json").write_text("[]")
        (case_dir / "judgement.json").write_text(json.dumps({"evidence_bound": False, "actionable": False}))
        summary_rows.append({"case": case})
    (out_dir / "summary.json").write_text(json.dumps({"mode": "model", "rows": summary_rows}))
    (out_dir / "VERIFIER_ONLY_EXPERIMENT_REPORT.md").write_text("# report\n")

    proc = subprocess.run(
        [sys.executable, "validate_verifier_only_eval.py", str(out_dir)],
        cwd=_BUILD_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert any("parse error" in problem for case in payload["cases"] for problem in case["problems"])
