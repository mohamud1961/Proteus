"""Vision perception lane: real semantic extraction for binary artifacts.

Root cause (code-from-image, 120 wasted steps): inspect_artifact could only
return a binary hex preview, no vision route existed, and the solver looped on
"successful" non-observations.  Now: with a vision model, metadata-only image
inspections are transcribed with honest model-transcription provenance;
without one, the inspection stays a failed capability gap.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Mapping

from aether_next.kernel import AetherNextKernel
from aether_next.model_hooks import ModelHooks
from aether_next.perception_vision import media_type_for
from aether_next.real_executor import SubprocessExecutor
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f030005fe02fea72d1e480000000049454e44ae426082"
)


class _StubVision:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt: str, image_b64: str, media_type: str) -> str:
        self.calls.append((media_type, image_b64[:16]))
        return "def main():\n    print('transcribed code')\n"


def _env(root: str) -> EnvMap:
    return EnvMap(
        task_prompt="Transcribe the code in code.png into main.py.",
        workspace_root=root,
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
            "artifact_inspection": CapabilityDescriptor(
                "artifact_inspection", "Inspect artifacts", tool_names=("inspect_artifact",),
            ),
        },
    )


class _InspectHooks:
    def __init__(self, vision) -> None:
        self._done = False
        if vision is not None:
            hooks = ModelHooks(lambda m, **k: "{}", lambda m, **k: "{}", vision_model=vision)
            self.perceive_image = hooks.perceive_image

    def architect(self, request: Mapping[str, object]) -> RuntimeConfigIR:
        return RuntimeConfigIR(
            architect_summary="vision test",
            solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem", "artifact_inspection"),
        )

    def solve(self, messages, compiled) -> SolverTurn:
        if not self._done:
            self._done = True
            return SolverTurn(kind="act", summary="inspect image", actions=(ActionRequest(
                action_id="a-see", kind="inspect_artifact", capability_id="artifact_inspection",
                arguments={"path": "code.png", "mode": "image"},
                intent="read the code from the image", expected_observation="code text",
                if_fail_next="report blocker",
            ),))
        return SolverTurn(kind="submit_outcome", summary="done")


def test_vision_model_transcribes_metadata_only_image_inspection() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "code.png").write_bytes(_PNG_BYTES)
        vision = _StubVision()
        hooks = _InspectHooks(vision)
        result = AetherNextKernel(max_steps=2).run(_env(root), SubprocessExecutor(root), hooks)

        inspect = next(r for r in result.receipts if r.kind == "artifact_inspection")
        assert inspect.success is True
        assert "transcribed code" in inspect.payload["extracted_text"]
        assert inspect.payload["extraction_route"] == "vision_model"
        assert inspect.payload["extraction_authority"] == "model_transcription_not_ground_truth"
        assert inspect.payload["metadata"]["semantic_content_available"] is True
        assert vision.calls and vision.calls[0][0] == "image/png"


def test_without_vision_model_the_gap_stays_honest() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "code.png").write_bytes(_PNG_BYTES)
        hooks = _InspectHooks(None)
        result = AetherNextKernel(max_steps=2).run(_env(root), SubprocessExecutor(root), hooks)

        inspect = next(r for r in result.receipts if r.kind == "artifact_inspection")
        assert inspect.success is False, "metadata-only image inspection must not read as success"
        assert inspect.payload["metadata"].get("semantic_content_available") is False
        assert inspect.failure_class == "perception_required"


def test_vision_failure_is_reported_not_swallowed() -> None:
    class _Failing:
        def __call__(self, prompt, image_b64, media_type):
            raise RuntimeError("HTTP 429 rate limited")

    with tempfile.TemporaryDirectory() as root:
        Path(root, "code.png").write_bytes(_PNG_BYTES)
        hooks = _InspectHooks(_Failing())
        result = AetherNextKernel(max_steps=2).run(_env(root), SubprocessExecutor(root), hooks)

        inspect = next(r for r in result.receipts if r.kind == "artifact_inspection")
        assert inspect.success is False
        assert "429" in inspect.summary
        assert inspect.payload["vision_error"]


def test_media_type_mapping() -> None:
    assert media_type_for("a/b/code.PNG") == "image/png"
    assert media_type_for("x.jpeg") == "image/jpeg"
    assert media_type_for("archive.tar.gz") == ""


def test_verifier_perceive_artifact_inspection() -> None:
    """The verifier can form its OWN vision reading, independent of any
    solver-produced transcription."""
    from aether_next.compiler import CapabilityRegistry, ConfigCompiler
    from aether_next.ledger import ExecutionLedger
    from aether_next.verifier_inspector import (
        VerifierInspectionRequest,
        execute_verifier_inspection_requests,
    )

    with tempfile.TemporaryDirectory() as root:
        Path(root, "code.png").write_bytes(_PNG_BYTES)
        env = _env(root)
        ir = RuntimeConfigIR(
            architect_summary="v", solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem"),
        )
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
        hooks = _InspectHooks(_StubVision())
        results = execute_verifier_inspection_requests(
            (VerifierInspectionRequest(request_id="v1", kind="perceive_artifact", path="code.png"),),
            compiled=compiled, ledger=ExecutionLedger(),
            executor=SubprocessExecutor(root), envmap=env, hooks=hooks,
        )
        row = results[0]
        assert "transcribed code" in row["transcription"]
        assert row["extraction_authority"] == "model_transcription_not_ground_truth"
        assert row["read_only"] is True

        # Without a vision route: explicit error, never a fake reading.
        no_vision = execute_verifier_inspection_requests(
            (VerifierInspectionRequest(request_id="v2", kind="perceive_artifact", path="code.png"),),
            compiled=compiled, ledger=ExecutionLedger(),
            executor=SubprocessExecutor(root), envmap=env, hooks=object(),
        )
        assert "error" in no_vision[0]
