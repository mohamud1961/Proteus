"""Regression test for RC1: the per-step solver context packet rendered ONLY
run_command receipts (via the narrow `command_results` selector/section).
Results from service_probe, artifact_inspection (vision transcription),
read_file, process_launch, etc. never appeared in ANY rendered section, so
the solver acted blind to its own tool output and repeated the action.

Trace evidence: in a code-from-image run, a 90KB artifact_inspection vision
transcription was dropped from context (the step-1 packet was 561 chars
total). In a qemu-startup run, 22 service_probe "live" results were
invisible, causing a 15-probe loop.

This test fails on pre-fix code because:
  - `ContextCompiler._available_sections` only ever built a `command_results`
    section filtered to `receipt.kind == "run_command"`; under the default
    (no-recipe) context policy nothing else ever surfaced a non-run_command
    tool receipt.
  - `context_views.receipt_inline_view` had no rendering branch for
    `artifact_inspection` or `service_probe`, so even a receipt lucky enough
    to land in some other section would not show its meaningful payload.
"""
from __future__ import annotations

import json

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.context_compiler import ContextCompiler
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CompletionPolicy,
    EnvMap,
    RuntimeConfigIR,
)


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Transcribe diagram.png and confirm the guest service is reachable.",
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
            "shell": CapabilityDescriptor("shell", "Shell", tool_names=("run_command",)),
        },
    )


def _runtime() -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="tool result visibility regression",
        solver_identity_prompt="solver",
        selected_capabilities=("filesystem", "shell"),
        completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
    )


def test_artifact_inspection_transcription_and_service_probe_summary_are_visible_to_solver() -> None:
    env = _env()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_runtime(), env)
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)

    # A vision transcription is often the ENTIRE task input for an image
    # task. Pad it well past the 4000-char inline cap so truncation is also
    # exercised; the needle sits at the front so it survives the cap.
    transcription = "def add(a, b):\n    return a + b\n# " + ("x" * 5000)
    ledger.record(Receipt(
        receipt_id="insp-1",
        step=1,
        kind="artifact_inspection",
        success=True,
        summary=(
            f"vision transcription of diagram.png (12345 bytes, image/png): "
            f"{len(transcription)} chars extracted"
        ),
        payload={
            "path": "diagram.png",
            "mode": "vision",
            "extracted_text": transcription,
            "extraction_route": "vision_model",
            "extraction_authority": "model_transcription_not_ground_truth",
            "media_type": "image/png",
        },
    ))
    # state_change is left at its False default here, matching the real
    # PerceptionLane/vision_transcribe_receipt behavior for
    # artifact_inspection, and (for the probe below) deliberately NOT set to
    # True so this test isolates the tool_results mechanism under test rather
    # than riding into visibility via the unrelated recent_progress/
    # state_change channel that some receipts also happen to satisfy.
    ledger.record(Receipt(
        receipt_id="probe-1",
        step=2,
        kind="service_probe",
        success=True,
        summary="probe 127.0.0.1:6665: live",
        payload={
            "target": "127.0.0.1:6665",
            "service_name": "qemu-guest",
            "live": True,
            "detail": "tcp connect ok",
        },
    ))

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    tool_results = {row["receipt_id"]: row for row in packet.get("tool_results", [])}
    assert "insp-1" in tool_results, "artifact_inspection receipt missing from context packet entirely"
    assert "def add(a, b):" in tool_results["insp-1"].get("extracted_text", ""), (
        "vision transcription excerpt missing from the artifact_inspection row"
    )
    assert "probe-1" in tool_results, "service_probe receipt missing from context packet entirely"
    assert tool_results["probe-1"].get("summary") == "probe 127.0.0.1:6665: live"
    assert tool_results["probe-1"].get("target") == "127.0.0.1:6665"
    assert tool_results["probe-1"].get("live") is True

    # Belt-and-braces per the reported defect: the evidence must be visible
    # somewhere in what actually gets serialized to the model, not just in an
    # internal structure this test happens to know the name of.
    serialized = json.dumps(packet, sort_keys=True, default=str)
    assert "def add(a, b):" in serialized
    assert "probe 127.0.0.1:6665: live" in serialized
