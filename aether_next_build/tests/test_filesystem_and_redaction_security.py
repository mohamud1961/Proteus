"""Adversarial filesystem containment and normal-evidence redaction tests."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import pytest

from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.model_hooks import ModelOutputError
from aether_next.real_executor import SubprocessExecutor, _resolve_safe
from aether_next.runtime_ir import CapabilityDescriptor, EnvMap, RuntimeConfigIR, SolverTurn


def test_sibling_prefix_path_is_not_inside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "app"
    sibling = tmp_path / "app_evil"
    root.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(PermissionError, match="escapes workspace"):
        _resolve_safe(str(root), str(sibling / "secret.txt"))


def test_parent_traversal_read_and_write_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    executor = SubprocessExecutor(str(root))

    with pytest.raises(PermissionError):
        executor.read_file("../outside.txt")
    with pytest.raises(PermissionError):
        executor.write_file("../outside.txt", "owned")
    assert outside.read_text(encoding="utf-8") == "outside"


def test_symlink_escape_read_and_write_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "secret-link.txt").symlink_to(secret)
    executor = SubprocessExecutor(str(root))

    with pytest.raises(PermissionError):
        executor.read_file("escape/secret.txt")
    with pytest.raises(PermissionError):
        executor.write_file("escape/new.txt", "owned")
    with pytest.raises(PermissionError, match="symlink"):
        executor.write_file("secret-link.txt", "owned")
    assert secret.read_text(encoding="utf-8") == "secret"
    assert not (outside / "new.txt").exists()


def test_glob_does_not_traverse_symlinked_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("leak", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    assert SubprocessExecutor(str(root)).glob("**/*.txt") == ()
    assert SubprocessExecutor(str(root)).glob("*.txt") == ("inside.txt",)


def test_command_cwd_outside_workspace_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    executor = SubprocessExecutor(str(root))

    with pytest.raises(PermissionError):
        executor.run_command("pwd", cwd=str(outside))


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Create out.txt.",
        workspace_root="/app",
        capabilities={"filesystem": CapabilityDescriptor("filesystem", "files")},
    )


class _MalformedThenSubmit:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls = 0
        self.last_raw_solver_output = ""

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return RuntimeConfigIR(
            architect_summary="test",
            solver_identity_prompt="solver",
            selected_capabilities=("filesystem",),
        )

    def solve(self, messages: list[dict[str, str]], compiled: Any) -> SolverTurn:
        self.calls += 1
        self.last_raw_solver_output = self.raw
        raise ModelOutputError("invalid structured turn")


def test_malformed_solver_secret_is_redacted_from_normal_receipts() -> None:
    secret = "super-secret-value"
    raw = f'{{"password":"{secret}", broken'
    result = AetherNextKernel(max_steps=1).run(
        _env(), MemoryExecutor(workspace_root="/app"), _MalformedThenSubmit(raw),
    )
    receipts = [item for item in result.receipts if item.kind == "solver_parse_error"]
    assert receipts
    serialized = str([item.payload for item in receipts])
    assert secret not in serialized
    primary = receipts[0].payload
    assert "[REDACTED]" in primary["redacted_output"]
    assert "raw_output" not in primary
    assert primary["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert primary["redaction_events"]
    assert primary["raw_output_storage"] == "protected_provider_evidence_only"
