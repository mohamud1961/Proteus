"""Durable evidence, source identity and grader-infrastructure truth tests."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from aether_next.evidence_finalization import (
    copy_snapshot,
    directory_manifest,
    executing_source_identity,
    finalize_evidence_directory,
    sha256_file,
)
from aether_next.real_executor import StreamSpooler, SubprocessExecutor
from aether_next.runners.docker_runner import _checked_process


def test_snapshot_copy_retains_exact_bytes_symlinks_and_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "binary.bin").write_bytes(b"\x00\xffexact\n")
    (source / "nested").mkdir()
    (source / "nested" / "text.txt").write_text("hello", encoding="utf-8")
    (source / "link").symlink_to("nested/text.txt")

    destination = tmp_path / "evidence" / "initial_workspace"
    manifest = copy_snapshot(source, destination)

    assert (destination / "binary.bin").read_bytes() == b"\x00\xffexact\n"
    assert (destination / "link").is_symlink()
    assert (destination / "link").readlink().as_posix() == "nested/text.txt"
    assert manifest["aggregate_sha256"] == directory_manifest(destination)["aggregate_sha256"]
    manifest_path = destination.parent / "initial_workspace.manifest.json"
    assert manifest_path.is_file()


def test_stream_spools_export_by_content_hash_and_survive_source_removal(tmp_path: Path) -> None:
    spooler = StreamSpooler(inline_cap=1000)
    payload = "abcdefgh" * 200
    first_inline, first_path = spooler.finalize(payload, "stdout")
    second_inline, second_path = spooler.finalize(payload, "stderr")
    assert "omitted" in first_inline
    assert "omitted" in second_inline
    assert first_path and second_path

    exported = spooler.export_to(str(tmp_path / "spools"))
    assert exported["file_count"] == 2
    assert exported["unique_content_count"] == 1
    stored = {row["stored_path"] for row in exported["files"]}
    assert len(stored) == 1
    stored_path = Path(next(iter(stored)))
    assert stored_path.read_text(encoding="utf-8") == payload

    Path(first_path).unlink(missing_ok=True)
    Path(second_path).unlink(missing_ok=True)
    assert stored_path.read_text(encoding="utf-8") == payload
    assert sha256_file(exported["manifest_path"]) == exported["manifest_sha256"]


def test_executor_exports_real_overflow_streams(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = SubprocessExecutor(str(workspace))
    result = executor.run_command("python3 -c 'print(\"x\" * 1100000)'", timeout_s=30)
    assert result.stdout_overflow_path
    manifest = executor.export_spools(str(tmp_path / "durable-spools"))
    assert manifest["file_count"] >= 1
    assert any(Path(row["stored_path"]).is_file() for row in manifest["files"])


def test_final_marker_is_last_and_binds_required_checksums(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = evidence / "result_record.json"
    result.write_text('{"status":"completed"}', encoding="utf-8")
    snapshot = evidence / "snapshot"
    snapshot.mkdir()
    (snapshot / "out.txt").write_text("42", encoding="utf-8")

    result_hash_before = sha256_file(result)
    marker = finalize_evidence_directory(
        evidence,
        required_paths=(result, snapshot),
        metadata={"source_commit": "abc", "source_clean": True},
    )

    assert Path(marker["path"]).name == "FINALIZED.json"
    payload = json.loads(Path(marker["path"]).read_text(encoding="utf-8"))
    row = next(item for item in payload["required_evidence"] if item["path"] == str(result))
    assert row["sha256"] == result_hash_before
    assert sha256_file(result) == result_hash_before
    assert sha256_file(marker["path"]) == marker["sha256"]


def test_finalization_refuses_missing_required_evidence(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing required paths"):
        finalize_evidence_directory(
            tmp_path,
            required_paths=(tmp_path / "missing.json",),
            metadata={},
        )
    assert not (tmp_path / "FINALIZED.json").exists()


def test_checked_process_turns_nonzero_copy_into_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr="permission denied",
        ),
    )
    proc, error = _checked_process(
        ["docker", "cp", "/task/.", "container:/task"],
        label="grader_copy_task_surface",
        timeout=60,
    )
    assert proc is not None and proc.returncode == 1
    assert error is not None
    assert "grader_copy_task_surface" in error
    assert "permission denied" in error


def test_checked_process_reports_timeout_without_task_failure_laundering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    proc, error = _checked_process(
        ["docker", "cp", "/tests/.", "container:/tests"],
        label="grader_copy_tests_surface",
        timeout=60,
    )
    assert proc is None
    assert error is not None and "TimeoutExpired" in error


def test_executing_source_identity_is_self_derived_not_operator_supplied() -> None:
    package_root = Path(__file__).resolve().parents[1]
    identity = executing_source_identity(package_root)
    assert identity["source_manifest"]["aggregate_sha256"]
    assert identity["source_manifest"]["file_count"] > 0
    assert "commit" in identity or identity["git_available"] is False
    if identity.get("git_available"):
        assert len(identity["commit"]) == 40
        assert len(identity["tree"]) == 40
        assert isinstance(identity["clean"], bool)
