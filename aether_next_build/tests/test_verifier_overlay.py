"""Sandboxed verifier overlay: checks and fixtures execute against a copy,
the solver workspace is never mutated, and rollback is unconditional.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.ledger import ExecutionLedger
from aether_next.real_executor import SubprocessExecutor
from aether_next.runtime_ir import CapabilityDescriptor, EnvMap, RuntimeConfigIR
from aether_next.verifier_inspector import (
    VerifierInspectionRequest,
    execute_verifier_inspection_requests,
)
from aether_next.verifier_overlay import VerifierOverlay


def _env(root: str, hints: dict | None = None) -> EnvMap:
    return EnvMap(
        task_prompt="Produce out.txt.",
        workspace_root=root,
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
        },
        grader_hints=dict(hints or {}),
    )


def _compiled(env: EnvMap, *, check_plan: tuple[str, ...] = ()) -> object:
    ir = RuntimeConfigIR(
        architect_summary="overlay test",
        solver_identity_prompt="solver",
        verifier_identity_prompt="verifier",
        selected_capabilities=("shell", "filesystem"),
        check_plan=check_plan,
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)


def test_overlay_command_mutations_never_touch_solver_workspace() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "original.txt").write_text("solver data")
        executor = SubprocessExecutor(root)
        overlay = VerifierOverlay(executor, root)
        try:
            result = overlay.run_command(
                "cat original.txt && touch VERIFIER_MARKER && rm original.txt && echo mutated"
            )
            assert result["success"], result
            assert "solver data" in result["stdout"]
            # Overlay saw the mutation...
            check = overlay.run_command("ls")
            assert "VERIFIER_MARKER" in check["stdout"]
            assert "original.txt" not in check["stdout"]
            # ...but the solver workspace is untouched.
            assert Path(root, "original.txt").read_text() == "solver data"
            assert not Path(root, "VERIFIER_MARKER").exists()
        finally:
            overlay.teardown()


def test_write_fixture_lands_in_overlay_only_and_teardown_removes_everything() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "tool.py").write_text("import sys; print(sys.stdin.read().upper())")
        executor = SubprocessExecutor(root)
        overlay = VerifierOverlay(executor, root)
        fixture = overlay.write_fixture("fixtures/case1.txt", "verifier input\n")
        assert fixture.get("written"), fixture
        run = overlay.run_command("python3 tool.py < fixtures/case1.txt")
        assert run["success"]
        assert "VERIFIER INPUT" in run["stdout"]
        # Fixture never appears in the solver workspace.
        assert not Path(root, "fixtures").exists()
        overlay_root = overlay.overlay_root
        assert overlay_root and Path(overlay_root).exists()
        teardown = overlay.teardown()
        assert teardown["removed"] is True
        assert not Path(overlay_root).exists()
        # Idempotent rollback.
        assert overlay.teardown() == {"removed": False}


def test_fixture_path_escapes_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        overlay = VerifierOverlay(executor, root)
        try:
            result = overlay.write_fixture("../escape.txt", "nope")
            assert "error" in result
            assert not Path(root).parent.joinpath("escape.txt").exists()
        finally:
            overlay.teardown()


def test_rerun_check_inspection_routes_through_overlay() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "out.txt").write_text("OK")
        env = _env(root, hints={"verify_commands": ["test -e out.txt && touch check_side_effect"]})
        _, eval_index = ConfigCompiler(CapabilityRegistry.from_envmap(env)).analyze_envmap(env)
        check_id = eval_index.checks[0].check_id
        compiled = _compiled(env, check_plan=(check_id,))
        executor = SubprocessExecutor(root)
        overlay = VerifierOverlay(executor, root)
        try:
            results = execute_verifier_inspection_requests(
                (VerifierInspectionRequest(request_id="r1", kind="rerun_check", check_id=check_id),),
                compiled=compiled,
                ledger=ExecutionLedger(),
                executor=executor,
                envmap=env,
                overlay=overlay,
            )
            assert results[0]["success"] is True
            assert results[0]["executed_in"] == "verifier_overlay"
            # The check's side effect stayed in the overlay.
            assert not Path(root, "check_side_effect").exists()
        finally:
            overlay.teardown()


def test_rerun_check_without_overlay_is_an_explicit_error_not_a_workspace_run() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "out.txt").write_text("OK")
        env = _env(root, hints={"verify_commands": ["touch must_not_exist"]})
        _, eval_index = ConfigCompiler(CapabilityRegistry.from_envmap(env)).analyze_envmap(env)
        check_id = eval_index.checks[0].check_id
        compiled = _compiled(env, check_plan=(check_id,))
        results = execute_verifier_inspection_requests(
            (VerifierInspectionRequest(request_id="r1", kind="rerun_check", check_id=check_id),),
            compiled=compiled,
            ledger=ExecutionLedger(),
            executor=SubprocessExecutor(root),
            envmap=env,
            overlay=None,
        )
        assert "error" in results[0]
        assert not Path(root, "must_not_exist").exists()
