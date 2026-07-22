"""Docker-exec-backed Executor (extracted from docker_runner for the 500-LOC cap).

Commands run inside the long-lived task container via ``docker exec``; file
operations act on the host bind-mounted workspace.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from ..execution import (
    ArtifactInspection,
    CommandResult,
    ProcessHandle,
    ProbeResult,
)
from ..real_executor import (
    StreamSpooler,
    SubprocessExecutor,
    _decode_partial,
)
from ..runtime_ir import EnvMap
from ..workspace_state import capture_workspace_state, diff_workspace_states

import logging
import re


class DockerExecExecutor:
    """Executor that runs commands inside a Docker container via ``docker exec``.

    File operations (read/write/exists/glob/inspect/refresh_envmap) operate
    on the HOST ``workspace_root_host`` directory, which is bind-mounted
    into the container at ``container_workdir``.
    """

    def __init__(
        self,
        container_id: str,
        workspace_root_host: str,
        *,
        default_timeout_s: int = 120,
        container_workdir: str = "/app",
    ) -> None:
        self._container_id = container_id
        self._host_root = str(Path(workspace_root_host).resolve())
        self._default_timeout_s = max(1, default_timeout_s)
        self._container_workdir = container_workdir
        self._host_exec = SubprocessExecutor(
            self._host_root, default_timeout_s=default_timeout_s,
        )
        self._spooler = StreamSpooler()
        self._process_registry: dict[str, ProcessHandle] = {}

    # ---- Filesystem (host-side, bind-mounted) --------------------------------

    def read_file(self, path: str) -> str:
        return self._host_exec.read_file(path)

    def read_file_bytes(self, path: str) -> bytes:
        return self._host_exec.read_file_bytes(path)

    def write_file(self, path: str, content: str) -> None:
        self._host_exec.write_file(path, content)

    def exists(self, path: str) -> bool:
        return self._host_exec.exists(path)

    def glob(self, pattern: str) -> tuple[str, ...]:
        return self._host_exec.glob(pattern)

    def inspect_artifact(self, path: str, mode: str) -> ArtifactInspection:
        return self._host_exec.inspect_artifact(path, mode)

    def refresh_envmap(self, envmap: EnvMap) -> EnvMap:
        return self._host_exec.refresh_envmap(envmap)

    # ---- Command execution (inside Docker container) -------------------------

    def for_workspace(self, workspace_root: str) -> "DockerExecExecutor":
        """Return an executor for a trusted isolated path in this container."""
        return DockerExecExecutor(
            self._container_id,
            self._host_root,
            default_timeout_s=self._default_timeout_s,
            container_workdir=workspace_root,
        )

    def export_spools(self, destination: str) -> dict[str, Any]:
        return self._spooler.export_to(destination)

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: int = 30,
    ) -> CommandResult:
        effective_timeout = timeout_s if timeout_s > 0 else self._default_timeout_s
        effective_cwd = cwd or self._container_workdir

        before_state = capture_workspace_state(self._host_root)

        docker_cmd = [
            "docker", "exec", "-w", effective_cwd,
            self._container_id,
            "bash", "-lc", command,
        ]
        timed_out = False
        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True, errors="replace",
                timeout=effective_timeout,
            )
            exit_code = proc.returncode
            raw_stdout, raw_stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            raw_stdout = _decode_partial(exc.stdout)
            raw_stderr = _decode_partial(exc.stderr) + (
                f"\n[harness] docker exec timed out after {effective_timeout}s; "
                "partial output above is preserved"
            )

        stdout_total, stderr_total = len(raw_stdout), len(raw_stderr)
        stdout, stdout_overflow = self._spooler.finalize(raw_stdout, "stdout")
        stderr, stderr_overflow = self._spooler.finalize(raw_stderr, "stderr")

        after_state = capture_workspace_state(self._host_root)
        state_delta = diff_workspace_states(before_state, after_state)
        modified = sorted(
            set(state_delta["content_changed_paths"])
            | set(state_delta["metadata_changed_paths"])
        )
        produced = tuple(state_delta["created_paths"])
        removed = tuple(state_delta["removed_paths"])

        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            modified_paths=tuple(modified),
            produced_artifacts=produced,
            removed_paths=removed,
            state_delta=state_delta,
            metrics={},
            stdout_overflow_path=stdout_overflow,
            stderr_overflow_path=stderr_overflow,
            stdout_bytes_total=stdout_total,
            stderr_bytes_total=stderr_total,
            timed_out=timed_out,
        )

    # ---- Process management (inside Docker container) ------------------------

    def launch_process(
        self,
        name: str,
        command: str,
        *,
        interactive: bool = False,
        cwd: str | None = None,
    ) -> ProcessHandle:
        effective_cwd = cwd or self._container_workdir
        launch_nonce = uuid.uuid4().hex[:12]
        command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
        stdout_log = f"/tmp/aether-processes/{launch_nonce}.stdout.log"
        stderr_log = f"/tmp/aether-processes/{launch_nonce}.stderr.log"
        wrapper = (
'set -eu\nmkdir -p /tmp/aether-processes\nnohup bash -lc "$1" >"$2" 2>"$3" </dev/null &\npid=$!\ni=0\nwhile [ $i -lt 20 ] && [ ! -r /proc/$pid/stat ]; do i=$((i+1)); sleep 0.05; done\nstart=$(awk \'{print $22}\' /proc/$pid/stat 2>/dev/null || true)\nif [ -z "$start" ]; then exit 42; fi\nprintf \'%s\\t%s\\n\' "$pid" "$start"\n'
        )
        docker_cmd = [
            "docker", "exec", "-w", effective_cwd,
            self._container_id,
            "bash", "-lc", wrapper, "_", command, stdout_log, stderr_log,
        ]
        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                check=True,
            )
            fields = proc.stdout.strip().split("\t")
            if len(fields) != 2 or not fields[0].isdigit() or not fields[1].isdigit():
                raise ValueError(f"invalid launch identity: {proc.stdout!r}")
            pid = int(fields[0])
            start_ticks = fields[1]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
            detail = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                detail = str(exc.stderr or exc.stdout or exc)
            return ProcessHandle(
                process_id=f"failed:{launch_nonce}",
                name=name,
                command=command,
                interactive=interactive,
                live=False,
                detail=f"process launch failed: {detail}",
                command_sha256=command_sha256,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
            )

        generation = hashlib.sha256(
            f"{self._container_id}\0{pid}\0{start_ticks}\0{command_sha256}".encode("utf-8")
        ).hexdigest()[:24]
        process_id = f"process:{generation}"
        handle = ProcessHandle(
            process_id=process_id,
            name=name,
            command=command,
            interactive=interactive,
            live=True,
            detail=f"pid={pid} start_ticks={start_ticks} container={self._container_id[:12]}",
            pid=pid,
            start_time_ticks=start_ticks,
            command_sha256=command_sha256,
            process_generation=generation,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )
        # A new launch generation supersedes earlier registered generations of
        # the same service name for proof purposes, even if the old OS process
        # remains alive until explicitly stopped.
        self._process_registry[process_id] = handle
        return handle

    def probe_process(self, target: str) -> ProbeResult:
        """Probe a live service endpoint or named process inside the container.

        ``probe_service`` is the solver-visible affordance for service liveness.
        A target shaped like ``host:port`` or ``port`` must test the TCP endpoint,
        not look for a process command line containing that literal string.
        """
        tcp_target = _parse_tcp_probe_target(target)
        if tcp_target is not None:
            return self._probe_tcp_endpoint(target, *tcp_target)
        return self._probe_process_name(target)

    def _registered_process(self, target: str) -> ProcessHandle | None:
        direct = self._process_registry.get(target)
        if direct is not None:
            return direct
        matches = [item for item in self._process_registry.values() if item.name == target]
        return matches[-1] if matches else None

    def _probe_process_name(self, target: str) -> ProbeResult:
        handle = self._registered_process(target)
        if handle is None or handle.pid is None or not handle.start_time_ticks:
            return ProbeResult(
                target=target,
                live=False,
                detail="no registered process generation",
                service_name=target,
            )
        code = (
            "import json,os,sys\n"
            f"pid={handle.pid!r}; expected={handle.start_time_ticks!r}\n"
            "path=f'/proc/{pid}/stat'\n"
            "try:\n"
            " data=open(path,encoding='utf-8').read().split()\n"
            " start=data[21]\n"
            " alive=start==expected\n"
            "except Exception as exc:\n"
            " print(json.dumps({'alive':False,'error':str(exc)})); sys.exit(1)\n"
            "print(json.dumps({'alive':alive,'pid':pid,'start_time_ticks':start}))\n"
            "sys.exit(0 if alive else 1)\n"
        )
        try:
            proc = subprocess.run(
                ["docker", "exec", self._container_id, "python3", "-c", code],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(target=target, live=False, detail="probe timed out", service_name=handle.name)
        alive = proc.returncode == 0
        return ProbeResult(
            target=target,
            live=alive,
            detail=(proc.stdout or proc.stderr).strip() or ("live" if alive else "not live"),
            service_name=handle.name,
            process_id=handle.process_id,
            process_generation=handle.process_generation,
            process_generation_verified=alive,
            endpoint_owner_pids=((handle.pid,) if alive and handle.pid is not None else ()),
        )

    def _probe_tcp_endpoint(self, target: str, host: str, port: int) -> ProbeResult:
        # Resolve listener socket inode(s) and owning PID(s) from /proc using
        # only Python stdlib.  Liveness without a registered owner is reported
        # but cannot satisfy a service-generation obligation.
        code = r'''import json, os, socket, sys
port = int(sys.argv[1])
inodes = set()
for table in ('/proc/net/tcp', '/proc/net/tcp6'):
    try:
        lines = open(table, encoding='utf-8').read().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        parts = line.split()
        if len(parts) < 10 or parts[3] != '0A':
            continue
        local = parts[1]
        try:
            local_port = int(local.rsplit(':', 1)[1], 16)
        except Exception:
            continue
        if local_port == port:
            inodes.add(parts[9])
owners = set()
if inodes:
    for pid_name in os.listdir('/proc'):
        if not pid_name.isdigit():
            continue
        fd_dir = f'/proc/{pid_name}/fd'
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in names:
            try:
                link = os.readlink(f'{fd_dir}/{fd}')
            except OSError:
                continue
            if link.startswith('socket:[') and link[8:-1] in inodes:
                owners.add(int(pid_name)); break
print(json.dumps({'live': bool(inodes), 'owner_pids': sorted(owners), 'inodes': sorted(inodes)}))
sys.exit(0 if inodes else 1)
'''
        try:
            proc = subprocess.run(
                ["docker", "exec", self._container_id, "python3", "-c", code, str(port)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(target=target, live=False, detail="tcp ownership probe timed out", service_name=target)
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        live = bool(payload.get("live", False)) and proc.returncode == 0
        owner_pids = tuple(int(item) for item in payload.get("owner_pids", ()) if str(item).isdigit())
        registered = [
            item for item in self._process_registry.values()
            if item.pid in owner_pids and item.live
        ]
        # Latest registered owner wins only when its PID/start generation is
        # still current.  Verify that exact identity before binding endpoint.
        owner = registered[-1] if registered else None
        if owner is not None:
            verified = self._probe_process_name(owner.process_id)
            if not verified.live:
                owner = None
        return ProbeResult(
            target=target,
            live=live,
            detail=json.dumps(payload, sort_keys=True),
            service_name=(owner.name if owner is not None else target),
            process_id=(owner.process_id if owner is not None else ""),
            process_generation=(owner.process_generation if owner is not None else ""),
            process_generation_verified=bool(owner is not None and live),
            endpoint_owner_pids=owner_pids,
        )

    def stop_process(self, target: str) -> bool:
        handle = self._registered_process(target)
        if handle is None or handle.pid is None or not handle.start_time_ticks:
            return False
        # Verify PID reuse has not occurred before signalling the exact process.
        probe = self._probe_process_name(handle.process_id)
        if not probe.live:
            self._process_registry[handle.process_id] = ProcessHandle(
                **{**handle.__dict__, "live": False, "detail": "already not live"}
            )
            return False
        code = (
            "import os,signal,sys,time\n"
            f"pid={handle.pid!r}; expected={handle.start_time_ticks!r}\n"
            "def current():\n"
            " try: return open(f'/proc/{pid}/stat',encoding='utf-8').read().split()[21]\n"
            " except Exception: return ''\n"
            "if current()!=expected: sys.exit(2)\n"
            "os.kill(pid, signal.SIGTERM)\n"
            "for _ in range(20):\n"
            " if current()!=expected: sys.exit(0)\n"
            " time.sleep(0.05)\n"
            "os.kill(pid, signal.SIGKILL)\n"
            "sys.exit(0)\n"
        )
        try:
            proc = subprocess.run(
                ["docker", "exec", self._container_id, "python3", "-c", code],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return False
        stopped = proc.returncode == 0
        if stopped:
            self._process_registry[handle.process_id] = ProcessHandle(
                **{**handle.__dict__, "live": False, "detail": "stopped"}
            )
        return stopped



_log = logging.getLogger(__name__)


def _parse_tcp_probe_target(target: str) -> tuple[str, int] | None:
    raw = str(target or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        port = int(raw)
        if 0 < port <= 65535:
            return ("127.0.0.1", port)
        return None
    if any(ch.isspace() for ch in raw):
        return None
    host, sep, port_text = raw.rpartition(":")
    if not sep or not port_text.isdigit():
        return None
    clean_host = host.strip() or "127.0.0.1"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", clean_host):
        return None
    port = int(port_text)
    if not 0 < port <= 65535:
        return None
    return clean_host, port
