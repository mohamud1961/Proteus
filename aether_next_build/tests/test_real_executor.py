"""Tests for SubprocessExecutor against a real temporary workspace."""
from __future__ import annotations

import os
import tempfile

import pytest

from aether_next.real_executor import SubprocessExecutor
from aether_next.runtime_ir import EnvMap, CapabilityDescriptor


@pytest.fixture()
def workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture()
def executor(workspace: str) -> SubprocessExecutor:
    return SubprocessExecutor(workspace, default_timeout_s=30)


class TestReadWriteRoundtrip:
    def test_write_then_read(self, executor: SubprocessExecutor, workspace: str) -> None:
        executor.write_file("hello.txt", "world")
        content = executor.read_file("hello.txt")
        assert content == "world"

    def test_write_nested(self, executor: SubprocessExecutor) -> None:
        executor.write_file("sub/dir/file.txt", "nested")
        assert executor.read_file("sub/dir/file.txt") == "nested"

    def test_write_overwrites_a_read_only_existing_file(
        self, executor: SubprocessExecutor, workspace: str,
    ) -> None:
        """Regression for the openssl-selfsigned-cert environment bug found while
        verifying the Slice A repair-slice rerun: the workspace is bind-mounted
        into a Docker container that runs as root, so a run_command executed via
        docker exec can create/overwrite a file at this path as root before a
        later write_file call runs. Reproduced empirically on the VM: plain
        open(path, "w") on a file this process doesn't have write permission on
        raises PermissionError even though this process owns the enclosing
        directory (overwriting file CONTENTS needs permission on the file, not
        the directory). Approximated here with chmod 0o444 (unwritable by the
        owner) rather than true cross-UID ownership, since creating a root-owned
        file from a non-root test process isn't portable -- this reproduces the
        same permission-denied-on-write failure class the real bug hit.
        """
        path = os.path.join(workspace, "check_cert.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("original")
        os.chmod(path, 0o444)  # read-only, as a root-written file would appear
        if os.access(path, os.W_OK):
            pytest.skip("running as a user that bypasses file permission bits (e.g. root)")

        executor.write_file("check_cert.py", "overwritten")

        assert executor.read_file("check_cert.py") == "overwritten"

    def test_read_missing_raises(self, executor: SubprocessExecutor) -> None:
        with pytest.raises(FileNotFoundError):
            executor.read_file("nonexistent.txt")


class TestRunCommand:
    def test_echo(self, executor: SubprocessExecutor) -> None:
        result = executor.run_command("echo hi")
        assert result.exit_code == 0
        assert result.success
        assert "hi" in result.stdout

    def test_creates_file_in_produced_artifacts(
        self, executor: SubprocessExecutor, workspace: str
    ) -> None:
        result = executor.run_command("echo content > newfile.txt")
        assert result.exit_code == 0
        assert "newfile.txt" in result.produced_artifacts
        assert os.path.isfile(os.path.join(workspace, "newfile.txt"))

    def test_false_command(self, executor: SubprocessExecutor) -> None:
        result = executor.run_command("false")
        assert not result.success
        assert result.exit_code != 0

    def test_timeout(self, workspace: str) -> None:
        executor = SubprocessExecutor(workspace, default_timeout_s=30)
        result = executor.run_command("sleep 5", timeout_s=1)
        assert result.exit_code == 124
        assert not result.success
        assert "timed out" in result.stderr

    def test_modified_paths(self, executor: SubprocessExecutor) -> None:
        executor.write_file("existing.txt", "original")
        result = executor.run_command("echo modified > existing.txt")
        assert result.exit_code == 0
        assert "existing.txt" in result.modified_paths


class TestExistsAndGlob:
    def test_exists_true(self, executor: SubprocessExecutor) -> None:
        executor.write_file("present.txt", "here")
        assert executor.exists("present.txt")

    def test_exists_false(self, executor: SubprocessExecutor) -> None:
        assert not executor.exists("absent.txt")

    def test_glob_matches(self, executor: SubprocessExecutor) -> None:
        executor.write_file("a.py", "a")
        executor.write_file("b.py", "b")
        executor.write_file("c.txt", "c")
        matches = executor.glob("*.py")
        assert "a.py" in matches
        assert "b.py" in matches
        assert "c.txt" not in matches


class TestProcessLifecycle:
    def test_launch_probe_stop(self, executor: SubprocessExecutor) -> None:
        handle = executor.launch_process("sleeper", "sleep 60")
        assert handle.live
        assert handle.process_id.startswith("proc-")

        probe = executor.probe_process(handle.process_id)
        assert probe.live

        stopped = executor.stop_process(handle.process_id)
        assert stopped

        probe_after = executor.probe_process(handle.process_id)
        assert not probe_after.live

    def test_probe_not_found(self, executor: SubprocessExecutor) -> None:
        probe = executor.probe_process("nonexistent-id")
        assert not probe.live
        assert "not found" in probe.detail


class TestInspectArtifact:
    def test_text_mode(self, executor: SubprocessExecutor) -> None:
        executor.write_file("data.json", '{"key": "value"}')
        inspection = executor.inspect_artifact("data.json", "text")
        assert inspection.success
        assert "key" in inspection.extracted_text
        assert inspection.metadata.get("backend") == "basic"

    def test_missing_file(self, executor: SubprocessExecutor) -> None:
        inspection = executor.inspect_artifact("nope.bin", "binary")
        assert not inspection.success

    def test_binary_mode(self, executor: SubprocessExecutor, workspace: str) -> None:
        path = os.path.join(workspace, "blob.bin")
        with open(path, "wb") as fh:
            fh.write(b"\x00\x01\x02\x03")
        inspection = executor.inspect_artifact("blob.bin", "binary")
        assert inspection.success
        assert inspection.metadata.get("backend") == "basic"
        assert "size_bytes" in inspection.metadata


class TestRefreshEnvmap:
    def test_refresh_picks_up_new_file(self, executor: SubprocessExecutor) -> None:
        envmap = EnvMap(
            task_prompt="test",
            workspace_root="/fake",
            capabilities={
                "shell": CapabilityDescriptor(
                    capability_id="shell",
                    summary="run commands",
                )
            },
        )
        executor.write_file("new_file.txt", "hi")
        refreshed = executor.refresh_envmap(envmap)
        assert "new_file.txt" in refreshed.visible_files
        # Capabilities carried over.
        assert "shell" in refreshed.capabilities


class TestPathSafety:
    def test_path_escape_fails_closed(self, executor: SubprocessExecutor, workspace: str) -> None:
        with pytest.raises(PermissionError, match="escapes workspace"):
            executor.write_file("../../etc/passwd", "nope")
        assert not os.path.exists(os.path.join(workspace, "passwd"))
