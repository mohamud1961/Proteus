"""Generic verifier probes: live service/port/process and media/artifact
inspection, all read-only, all executed through the run's executor substrate.
"""
from __future__ import annotations

import http.server
import socket
import tempfile
import threading
from pathlib import Path

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.real_executor import SubprocessExecutor
from aether_next.runtime_ir import CapabilityDescriptor, EnvMap, RuntimeConfigIR
from aether_next.verifier_inspector import (
    VerifierInspectionRequest,
    execute_verifier_inspection_requests,
    parse_verifier_inspection_requests,
)
from aether_next.verifier_probes import (
    inspect_artifact_probe,
    probe_http,
    probe_port,
    probe_process,
)

# 1x1 transparent PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f030005fe02fea72d1e480000000049454e44ae426082"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ScriptedExecutor:
    def __init__(self, results: tuple[CommandResult, ...]) -> None:
        self._results = list(results)

    def run_command(self, command: str, *, timeout_s: int = 30) -> CommandResult:
        assert self._results, command
        return self._results.pop(0)


def test_probe_port_open_and_closed() -> None:
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        open_port = server.getsockname()[1]
        try:
            result = probe_port(executor, f"127.0.0.1:{open_port}")
            assert result["state"] == "open", result
        finally:
            server.close()
        closed = probe_port(executor, str(_free_port()))
        assert closed["state"] == "closed", closed
        bad = probe_port(executor, "not-a-port")
        assert "error" in bad


def test_probe_http_live_server_and_unreachable() -> None:
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"SERVICE_BODY_MARKER"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe_http(executor, f"http://127.0.0.1:{port}/")
            assert result["reachable"] is True
            assert result["status"] == 200
            assert "SERVICE_BODY_MARKER" in result["body_head"]
        finally:
            server.shutdown()
        dead = probe_http(executor, f"http://127.0.0.1:{_free_port()}/")
        assert dead["reachable"] is False


def test_probe_process_finds_live_process() -> None:
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        # This test's own interpreter is a guaranteed-live python process.
        result = probe_process(executor, "python")
        if result.get("state") == "unknown":
            assert result["running"] is False
            assert "tool_unavailable" in result["error"]
            return
        assert result["running"] is True
        assert result["match_count"] >= 1
        missing = probe_process(executor, "definitely_not_a_process_name_xyz123")
        assert missing["running"] is False


def test_probe_process_reports_unknown_when_process_listing_is_denied() -> None:
    executor = _ScriptedExecutor(
        (
            CommandResult(
                command="pgrep",
                exit_code=0,
                stderr="pgrep: Cannot get process list\nsysmon request failed with error: sysmond service not found\n",
            ),
            CommandResult(command="ps", exit_code=0, stderr="ps: operation not permitted\n"),
        )
    )
    result = probe_process(executor, "python")
    assert result["state"] == "unknown"
    assert result["running"] is False
    assert result["match_count"] == 0
    assert "tool_unavailable" in result["error"]


def test_inspect_artifact_probe_binary_and_text() -> None:
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        png = Path(root, "image.png")
        png.write_bytes(_PNG_BYTES)
        result = inspect_artifact_probe(executor, "image.png")
        assert result["exists"] is True
        assert "png" in result["file_type"].lower() or "image" in result["file_type"].lower()
        assert result["size_bytes"] == str(len(_PNG_BYTES))
        assert len(result["sha256"]) == 64

        Path(root, "notes.txt").write_text("ARTIFACT_TEXT_MARKER\nline two\n")
        text = inspect_artifact_probe(executor, "notes.txt")
        assert text["exists"] is True
        assert "ARTIFACT_TEXT_MARKER" in text.get("content_head", "")

        missing = inspect_artifact_probe(executor, "no_such_file.bin")
        assert missing["exists"] is False


def test_probe_kinds_dispatch_through_inspector_and_never_mutate() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "artifact.txt").write_text("data")
        env = EnvMap(
            task_prompt="Serve the thing.",
            workspace_root=root,
            capabilities={
                "shell": CapabilityDescriptor("shell", "Run commands"),
                "filesystem": CapabilityDescriptor("filesystem", "Files"),
            },
        )
        ir = RuntimeConfigIR(
            architect_summary="probe test",
            solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem"),
        )
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
        executor = SubprocessExecutor(root)
        before = sorted(p.name for p in Path(root).iterdir())
        results = execute_verifier_inspection_requests(
            (
                VerifierInspectionRequest(request_id="p1", kind="probe_process", target="python"),
                VerifierInspectionRequest(request_id="p2", kind="probe_port", target=str(_free_port())),
                VerifierInspectionRequest(request_id="p3", kind="inspect_artifact", path="artifact.txt"),
            ),
            compiled=compiled,
            ledger=ExecutionLedger(),
            executor=executor,
            envmap=env,
        )
        by_id = {row["request_id"]: row for row in results}
        if by_id["p1"].get("state") == "unknown":
            assert by_id["p1"]["running"] is False
            assert "tool_unavailable" in by_id["p1"]["error"]
        else:
            assert by_id["p1"]["running"] is True
        assert by_id["p2"]["state"] == "closed"
        assert by_id["p3"]["exists"] is True
        assert all(row.get("read_only") for row in results)
        assert sorted(p.name for p in Path(root).iterdir()) == before


def test_inspection_request_parser_accepts_first_json_object_only() -> None:
    raw = (
        '{"kind":"inspect","requests":[{"kind":"read_file","path":"out.txt","request_id":"r1"}]}'
        '{"verdict":"uncertain_missing_evidence","confidence":"high","summary":"later object"}'
    )
    requests = parse_verifier_inspection_requests(raw)
    assert len(requests) == 1
    assert requests[0].kind == "read_file"
    assert requests[0].path == "out.txt"


def test_inspection_request_parser_aliases_run_check_to_rerun_check() -> None:
    requests = parse_verifier_inspection_requests({
        "kind": "inspect",
        "requests": [{"kind": "run_check", "check_id": "c1", "request_id": "r1"}],
    })
    assert requests[0].kind == "rerun_check"


def test_read_file_inspection_supports_globbed_paths() -> None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "logs").mkdir()
        Path(root, "logs", "a.log").write_text("A\n")
        Path(root, "logs", "b.log").write_text("B\n")
        env = EnvMap(
            task_prompt="t",
            workspace_root=root,
            capabilities={
                "shell": CapabilityDescriptor("shell", "Run commands"),
                "filesystem": CapabilityDescriptor("filesystem", "Files"),
            },
        )
        ir = RuntimeConfigIR(
            architect_summary="s",
            solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem"),
        )
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
        results = execute_verifier_inspection_requests(
            (VerifierInspectionRequest(request_id="r", kind="read_file", path="logs/*.log", limit=2),),
            compiled=compiled,
            ledger=ExecutionLedger(),
            executor=SubprocessExecutor(root),
            envmap=env,
        )
        assert sorted(results[0]["matched_paths"]) == ["logs/a.log", "logs/b.log"]
        assert [row["excerpt"] for row in results[0]["matches"]] == ["A\n", "B\n"]


def test_read_file_inspection_supports_offset_paging() -> None:
    with tempfile.TemporaryDirectory() as root:
        content = "A" * 5000 + "WINDOW_MARKER" + "B" * 5000
        Path(root, "big.txt").write_text(content)
        env = EnvMap(
            task_prompt="t",
            workspace_root=root,
            capabilities={
                "shell": CapabilityDescriptor("shell", "Run commands"),
                "filesystem": CapabilityDescriptor("filesystem", "Files"),
            },
        )
        ir = RuntimeConfigIR(
            architect_summary="s",
            solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem"),
        )
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
        results = execute_verifier_inspection_requests(
            (VerifierInspectionRequest(request_id="r", kind="read_file", path="big.txt", offset=5000),),
            compiled=compiled,
            ledger=ExecutionLedger(),
            executor=SubprocessExecutor(root),
            envmap=env,
        )
        assert results[0]["bytes"] == len(content)
        assert results[0]["offset"] == 5000
        assert results[0]["excerpt"].startswith("WINDOW_MARKER")


def test_verifier_can_read_command_output_handles() -> None:
    with tempfile.TemporaryDirectory() as root:
        env = EnvMap(
            task_prompt="t",
            workspace_root=root,
            capabilities={
                "shell": CapabilityDescriptor("shell", "Run commands"),
                "filesystem": CapabilityDescriptor("filesystem", "Files"),
            },
        )
        ir = RuntimeConfigIR(
            architect_summary="s",
            solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem"),
        )
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
        ledger = ExecutionLedger()
        ledger.record(Receipt(
            receipt_id="step-5:a-1:cmd",
            step=5,
            kind="run_command",
            success=True,
            summary="command exit=0",
            payload={
                "stdout_handle": "5:a-1:stdout",
                "stdout_full": "OUTPUT_TOML\njump_takeoff_frame_number = 107\nSPOT_AUDIT frame 104 score=10.9\n",
                "stderr_handle": "5:a-1:stderr",
                "stderr_full": "",
            },
        ))

        results = execute_verifier_inspection_requests(
            (VerifierInspectionRequest(request_id="r", kind="read_output", handle="5:a-1:stdout", span=200),),
            compiled=compiled,
            ledger=ledger,
            executor=SubprocessExecutor(root),
            envmap=env,
        )

        assert results[0]["source_receipt_id"] == "step-5:a-1:cmd"
        assert results[0]["stream"] == "stdout"
        assert "SPOT_AUDIT frame 104" in results[0]["excerpt"]
        assert results[0]["read_only"] is True


def test_verifier_read_output_uses_spooled_full_stream() -> None:
    with tempfile.TemporaryDirectory() as root:
        spool = Path(root, "stdout.spool")
        spool.write_text("A" * 5000 + "FRAME_TRANSCRIPT_MARKER" + "B" * 5000)
        env = EnvMap(
            task_prompt="t",
            workspace_root=root,
            capabilities={
                "shell": CapabilityDescriptor("shell", "Run commands"),
                "filesystem": CapabilityDescriptor("filesystem", "Files"),
            },
        )
        ir = RuntimeConfigIR(
            architect_summary="s",
            solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem"),
        )
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
        ledger = ExecutionLedger()
        ledger.record(Receipt(
            receipt_id="step-5:a-1:cmd",
            step=5,
            kind="run_command",
            success=True,
            summary="command exit=0",
            payload={
                "stdout_handle": "5:a-1:stdout",
                "stdout_full": "TRUNCATED",
                "stdout_overflow_path": str(spool),
            },
        ))

        results = execute_verifier_inspection_requests(
            (VerifierInspectionRequest(request_id="r", kind="read_output", handle="5:a-1:stdout", offset=5000, span=80),),
            compiled=compiled,
            ledger=ledger,
            executor=SubprocessExecutor(root),
            envmap=env,
        )

        assert results[0]["bytes"] == spool.stat().st_size
        assert results[0]["offset"] == 5000
        assert results[0]["excerpt"].startswith("FRAME_TRANSCRIPT_MARKER")


def test_inspect_artifact_probe_reports_file_mode_and_owner() -> None:
    """Permissions are first-class verifiable state (live gap: a correct
    openssl run could not be verified because no read-only surface exposed
    the key file's mode 600)."""
    import os

    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        key = Path(root, "server.key")
        key.write_text("PRIVATE KEY")
        os.chmod(key, 0o600)
        result = inspect_artifact_probe(executor, "server.key")
        assert result["exists"] is True
        assert result["mode"] == "600", result
        assert result["owner"]
        assert result["mtime_epoch"].isdigit()
