"""Detached job registry for persistent background work."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import errno
import json
import os
import signal
import shlex
import subprocess
import time
import uuid

from harness.aether2.runtime.executor import ContainerBackend


TAIL_LIMIT = 2048


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    pid: int
    alive: bool
    exit_code: int | None
    cwd: str
    log_path: str
    tail: str
    registry_path: str


class JobRegistry:
    """Detached job registry. Jobs are launched through `backend` (C1): when
    `backend.kind == "local"` (the default), the wrapper script is spawned on
    the host exactly as before. When `backend.kind == "docker"`, the wrapper
    script is launched INSIDE the task container via `docker exec -d
    setsid ...`, so containerized tasks' services/jobs run where the agent
    (and the verifier) actually live, not on the host. Pidfiles/logfiles/
    registry state always live under `state_dir` (the task workspace's
    `.aether2/state` dir), which is bind-mounted into the container, so
    `status()` reads container truth either way.
    """

    def __init__(self, state_dir: Path, *, backend: ContainerBackend | None = None, container_path_fn=None) -> None:
        self.state_dir = state_dir.resolve()
        self.workspace_root = _infer_workspace_root(self.state_dir)
        self.jobs_dir = self.state_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.backend = backend or ContainerBackend()
        # Maps a host path to its in-container path string (required when
        # backend.kind != "local"); injected by the caller (ExecutionContext),
        # which already knows the workspace-root mapping.
        self._container_path_fn = container_path_fn

    _SPAWN_RETRY_MAX = 5
    _SPAWN_RETRY_BASE_SEC = 0.2

    def start(self, cmd: str, job_id: str | None = None, cwd: str | Path | None = None) -> str:
        command = str(cmd).strip()
        if not command:
            raise ValueError("empty_command: start_job requires a non-empty command")
        resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        job_dir = self.jobs_dir / resolved_job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        cwd_path = self._resolve_cwd(cwd)
        log_path = job_dir / "job.log"
        exit_code_path = job_dir / "exit_code"
        wrapper_path = job_dir / "run.sh"
        command_path = job_dir / "command.sh"
        pid_path = job_dir / "job.pid"
        meta_path = job_dir / "meta.json"
        execution_cwd = self._execution_path(cwd_path)
        execution_log_path = self._execution_path(log_path)
        execution_exit_code_path = self._execution_path(exit_code_path)
        execution_command_path = self._execution_path(command_path)

        with command_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(cmd)
        command_path.chmod(0o700)

        wrapper_path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env sh",
                    "set +e",
                    f"cd {json.dumps(execution_cwd)}",
                    (
                        "trap 'status=$?; "
                        f"if [ ! -f {json.dumps(execution_exit_code_path)} ]; then "
                        f"printf \"%s\\\\n\" \"$status\" > {json.dumps(execution_exit_code_path)}; "
                        "fi' EXIT"
                    ),
                    f". {json.dumps(execution_command_path)} >> {json.dumps(execution_log_path)} 2>&1",
                    "code=$?",
                    f"printf \"%s\\n\" \"$code\" > {json.dumps(execution_exit_code_path)}",
                    "exit \"$code\"",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        wrapper_path.chmod(0o755)

        if self.backend.kind == "local":
            pid = self._start_local(wrapper_path, cwd_path=cwd_path, job_dir=job_dir)
        else:
            pid = self._start_in_container(wrapper_path, job_dir)

        pid_path.write_text(f"{pid}\n", encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "job_id": resolved_job_id,
                    "cmd": command,
                    "cwd": str(cwd_path),
                    "pid": pid,
                    "log_path": str(log_path),
                    "registry_path": str(meta_path),
                    "exit_code_path": str(exit_code_path),
                    "backend_kind": self.backend.kind,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return resolved_job_id

    def _resolve_cwd(self, cwd: str | Path | None) -> Path:
        candidate = self.workspace_root if cwd is None else Path(cwd).expanduser()
        container_root = str(self.backend.container_workspace_root or "").rstrip("/")
        if not candidate.is_absolute():
            candidate = (self.workspace_root / candidate).resolve(strict=False)
        elif container_root:
            candidate_str = candidate.as_posix()
            if candidate_str == container_root or candidate_str.startswith(f"{container_root}/"):
                relative = candidate_str.removeprefix(container_root).lstrip("/")
                candidate = (self.workspace_root / relative).resolve(strict=False)
            else:
                candidate = candidate.resolve(strict=False)
        else:
            candidate = candidate.resolve(strict=False)
        if not _is_within_workspace(candidate, self.workspace_root):
            raise ValueError("workspace_boundary_violation: cwd must stay inside the task workspace")
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError("cwd_missing: cwd must exist as a directory inside the task workspace")
        return candidate

    def _start_local(self, wrapper_path: Path, *, cwd_path: Path, job_dir: Path) -> int:
        attempt = 0
        while True:
            try:
                with (job_dir / "launcher.out").open("ab") as stdout_handle, (job_dir / "launcher.err").open("ab") as stderr_handle:
                    proc = subprocess.Popen(
                        [str(wrapper_path)],
                        cwd=str(cwd_path),
                        start_new_session=True,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                    )
                return proc.pid
            except OSError as exc:
                attempt += 1
                if getattr(exc, "errno", None) != errno.EAGAIN:
                    raise
                if attempt >= self._SPAWN_RETRY_MAX:
                    raise
                time.sleep(self._SPAWN_RETRY_BASE_SEC * (2 ** (attempt - 1)))

    def _execution_path(self, path: Path) -> str:
        """Return the path string that should be used inside the job wrapper.

        Registry metadata remains host-auditable, but the wrapper script runs
        in the selected execution backend. For Docker-backed jobs, every path
        used by the script must be translated into the container namespace.
        """
        if self.backend.kind == "local":
            return str(path)
        if self._container_path_fn is None:
            raise RuntimeError("docker backend requires a container_path_fn to map host paths into the container")
        return str(self._container_path_fn(path))

    def _start_in_container(self, wrapper_path: Path, job_dir: Path) -> int:
        """Launch `wrapper_path` detached INSIDE the container (C1).

        Runs `docker exec <container> setsid <wrapper> & echo $!` via the
        container's shell so the launched process is reparented to the
        container's init and survives the agent process tree exiting, and
        prints its in-container PID to stdout for liveness checks.
        """
        if self.backend.kind != "docker":
            raise RuntimeError(f"unsupported non-local backend kind: {self.backend.kind!r}")
        if not self.backend.container_id:
            raise RuntimeError("docker backend requires a container_id")
        if self._container_path_fn is None:
            raise RuntimeError("docker backend requires a container_path_fn to map host paths into the container")
        container_wrapper = self._container_path_fn(wrapper_path)
        exec_cmd = [
            "docker",
            "exec",
            self.backend.container_id,
            self.backend.exec_shell,
            "-c",
            f"setsid {shlex.quote(container_wrapper)} </dev/null >/dev/null 2>&1 & echo $!",
        ]
        completed = subprocess.run(exec_cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"failed to launch in-container job: {completed.stderr.strip()}")
        pid_text = (completed.stdout or "").strip().splitlines()[-1] if completed.stdout else ""
        try:
            return int(pid_text)
        except ValueError:
            raise RuntimeError(f"failed to parse in-container job pid from output: {completed.stdout!r}")

    def _pid_alive_for_backend(self, pid: int) -> bool:
        if self.backend.kind == "local":
            return _pid_alive(pid)
        if not self.backend.container_id:
            return False
        completed = subprocess.run(
            ["docker", "exec", self.backend.container_id, "kill", "-0", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    def status(self, job_id: str) -> JobStatus:
        meta_path = self.jobs_dir / job_id / "meta.json"
        if not meta_path.exists():
            raise KeyError(f"unknown job: {job_id}")
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        exit_code_path = Path(data["exit_code_path"])
        exit_code = _read_exit_code(exit_code_path)
        process_alive = self._pid_alive_for_backend(pid)
        if exit_code is None and not process_alive:
            # The launcher process was terminated (e.g. by an external signal)
            # before it could record its own exit status. Report a truthful,
            # generic "terminated externally" code rather than leaving the
            # job stuck in an "alive" state forever.
            exit_code = 143
        alive = exit_code is None and process_alive
        return JobStatus(
            job_id=job_id,
            pid=pid,
            alive=alive,
            exit_code=exit_code,
            cwd=str(data["cwd"]),
            log_path=str(data["log_path"]),
            tail=_read_tail(Path(data["log_path"])),
            registry_path=str(meta_path),
        )

    def as_dict(self, job_id: str) -> dict[str, Any]:
        return asdict(self.status(job_id))

    def stop(self, job_id: str, *, timeout_sec: float = 3.0) -> JobStatus:
        """Terminate a registered detached job and return its final status."""

        status = self.status(job_id)
        if not status.alive:
            return status
        if self.backend.kind == "local":
            try:
                os.killpg(status.pid, signal.SIGTERM)
            except ProcessLookupError:
                return self.status(job_id)
            except PermissionError:
                os.kill(status.pid, signal.SIGTERM)
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                current = self.status(job_id)
                if not current.alive:
                    return current
                time.sleep(0.1)
            try:
                os.killpg(status.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(status.pid, signal.SIGKILL)
            return self.status(job_id)

        if not self.backend.container_id:
            return status
        subprocess.run(
            ["docker", "exec", self.backend.container_id, "kill", "-TERM", f"-{status.pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            current = self.status(job_id)
            if not current.alive:
                return current
            time.sleep(0.1)
        subprocess.run(
            ["docker", "exec", self.backend.container_id, "kill", "-KILL", f"-{status.pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return self.status(job_id)



def _infer_workspace_root(state_dir: Path) -> Path:
    """Infer the task workspace root from `.aether2/state`.

    Aether-2 constructs JobRegistry with `workspace/.aether2/state`. Detached
    jobs must default to the task workspace, not to `.aether2`, otherwise
    model-launched services and background work start in harness-private state
    rather than in the task workspace.
    """
    if state_dir.name == "state" and state_dir.parent.name == ".aether2":
        return state_dir.parent.parent.resolve()
    return state_dir.parent.resolve()


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return False
    return True

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_exit_code(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _read_tail(path: Path) -> str:
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="replace")
    return data[-TAIL_LIMIT:]
