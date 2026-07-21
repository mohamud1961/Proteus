"""Harbor-native Aether execution backend backed by BaseEnvironment.

Harbor remains the source of truth for task state. Commands execute through
``BaseEnvironment.exec`` and file reads/writes go through Harbor download/upload
operations. A local mirror is maintained only for snapshot diffs and trace
artifacts; it is never treated as the authoritative workspace.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import shlex
import subprocess
import tarfile
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
import time
from typing import TYPE_CHECKING, Any, Awaitable, Iterable, TypeVar

from harness.aether2.runtime.adaptive_profile import AgentInitializationFailure
from harness.aether2.runtime.executor import ContainerBackend, ContainerExecutor, RawResult
from harness.aether2.runtime.task_spec import TaskSpec
from harness.aether2.traces.envelope import ErrorInfo
from harness.aether2.traces.failure_cards import classify_failure

if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment


DEFAULT_HARBOR_WORKSPACE_PROBE_CANDIDATES = ("/app", "/workspace")
DEFAULT_PROBE_TIMEOUT_SEC = 30
DEFAULT_HARBOR_MIRROR_EXCLUDES = (
    ".aether2/harbor_jobs/*",
    "*.iso",
    "*.qcow2",
    "*.img",
    "*.raw",
    "*.vmdk",
    "*.mp4",
    "*.mov",
    "*.avi",
    "*.mkv",
)
TERMINAL_MODEL_LIMITED_CLASSES = (
    "MODEL_CAPABILITY",
    "MODEL_VARIANCE",
    "PERCEPTION_SUBSTRATE",
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class HarborWorkspaceProbe:
    """Observed Harbor workspace facts for adapter setup and executor wiring."""

    pwd: str
    git_root: str | None
    workspace_root: str
    existing_candidates: tuple[str, ...]


@dataclass(frozen=True)
class HarborJobStatus:
    job_id: str
    pid: int
    alive: bool
    exit_code: int | None
    cwd: str
    log_path: str
    tail: str
    registry_path: str


@dataclass(frozen=True)
class HarborSessionRecord:
    session_id: str
    command: str
    remote_dir: str
    remote_input_path: str
    remote_screen_path: str
    remote_pid_path: str
    registry_path: str


class HarborSessionRegistry:
    """FIFO/log-backed interactive sessions inside a Harbor task workspace.

    This is a conservative Harbor-native substitute for tmux/docker sessions.
    It is not a full terminal emulator, but it gives line-oriented interactive
    programs a persistent stdin stream and readable output log without requiring
    a Docker container id.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        environment: "BaseEnvironment | Any",
        remote_workspace_root: str,
        default_env: dict[str, str] | None = None,
    ) -> None:
        self.state_dir = state_dir.resolve()
        self.sessions_dir = self.state_dir / "harbor_sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.environment = environment
        self.remote_workspace_root = remote_workspace_root.rstrip("/") or "/app"
        self.default_env = dict(default_env or {})

    def start(self, session_id: str, command: str) -> str:
        safe_session_id = _safe_identifier(session_id)
        path = self.sessions_dir / f"{safe_session_id}.json"
        if path.exists():
            raise ValueError(f"session already exists: {safe_session_id}")
        remote_dir = f"{self.remote_workspace_root}/.aether2/harbor_sessions/{safe_session_id}"
        remote_input = f"{remote_dir}/input.fifo"
        remote_screen = f"{remote_dir}/screen.log"
        remote_exit_code = f"{remote_dir}/exit_code"
        remote_pid = f"{remote_dir}/session.pid"
        remote_command = f"{remote_dir}/command.sh"
        remote_wrapper = f"{remote_dir}/run.sh"

        _run_awaitable_sync(
            self.environment.exec(
                command=f"mkdir -p {shlex.quote(remote_dir)} && rm -f {shlex.quote(remote_input)} && mkfifo {shlex.quote(remote_input)} && : > {shlex.quote(remote_screen)}",
                cwd=self.remote_workspace_root,
                env=self.default_env or None,
                timeout_sec=30,
            )
        )
        command_local = self.sessions_dir / f"{safe_session_id}.command.sh"
        wrapper_local = self.sessions_dir / f"{safe_session_id}.run.sh"
        try:
            command_local.write_text(command, encoding="utf-8")
            command_local.chmod(0o700)
            wrapper_local.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env sh",
                        "set +e",
                        f"cd {shlex.quote(self.remote_workspace_root)}",
                        (
                            "nohup sh -c "
                            + shlex.quote(
                                (
                                    f"tail -f {shlex.quote(remote_input)} | "
                                    f"sh -lc {shlex.quote(command)} > {shlex.quote(remote_screen)} 2>&1; "
                                    f"printf \"%s\\n\" \"$?\" > {shlex.quote(remote_exit_code)}"
                                )
                            )
                            + " </dev/null >/dev/null 2>&1 &"
                        ),
                        f"printf \"%s\\n\" \"$!\" > {shlex.quote(remote_pid)}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            wrapper_local.chmod(0o700)
            _run_awaitable_sync(self.environment.upload_file(command_local, remote_command))
            _run_awaitable_sync(self.environment.upload_file(wrapper_local, remote_wrapper))
        finally:
            command_local.unlink(missing_ok=True)
            wrapper_local.unlink(missing_ok=True)

        _run_awaitable_sync(
            self.environment.exec(
                command=f"sh {shlex.quote(remote_wrapper)}",
                cwd=self.remote_workspace_root,
                env=self.default_env or None,
                timeout_sec=30,
            )
        )
        record = HarborSessionRecord(
            session_id=safe_session_id,
            command=command,
            remote_dir=remote_dir,
            remote_input_path=remote_input,
            remote_screen_path=remote_screen,
            remote_pid_path=remote_pid,
            registry_path=str(path),
        )
        path.write_text(json.dumps(record.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return safe_session_id

    def send(self, session_id: str, keys: str) -> None:
        record = self._load(session_id)
        payload = _translate_session_keys(keys)
        temp_path = self.sessions_dir / f"{record.session_id}.{uuid.uuid4().hex}.input"
        try:
            temp_path.write_text(payload, encoding="utf-8")
            remote_temp = f"{record.remote_dir}/send-{uuid.uuid4().hex}.txt"
            _run_awaitable_sync(self.environment.upload_file(temp_path, remote_temp))
            _run_awaitable_sync(
                self.environment.exec(
                    command=f"cat {shlex.quote(remote_temp)} > {shlex.quote(record.remote_input_path)}; rm -f {shlex.quote(remote_temp)}",
                    cwd=self.remote_workspace_root,
                    env=self.default_env or None,
                    timeout_sec=10,
                )
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def read(self, session_id: str) -> str:
        record = self._load(session_id)
        completed = _run_awaitable_sync(
            self.environment.exec(
                command=f"cat {shlex.quote(record.remote_screen_path)} 2>/dev/null || true",
                cwd=self.remote_workspace_root,
                env=self.default_env or None,
                timeout_sec=10,
            )
        )
        return str(getattr(completed, "stdout", "") or "")

    def stop(self, session_id: str) -> None:
        record = self._load(session_id)
        _run_awaitable_sync(
            self.environment.exec(
                command=(
                    f"pid=$(cat {shlex.quote(record.remote_pid_path)} 2>/dev/null || true); "
                    "if test -n \"$pid\"; then "
                    "children=$(ps -o pid= --ppid \"$pid\" 2>/dev/null || true); "
                    "if test -n \"$children\"; then kill $children 2>/dev/null || true; fi; "
                    "kill \"$pid\" 2>/dev/null || true; "
                    "fi; "
                    f"rm -rf {shlex.quote(record.remote_dir)}"
                ),
                cwd=self.remote_workspace_root,
                env=self.default_env or None,
                timeout_sec=30,
            )
        )
        Path(record.registry_path).unlink(missing_ok=True)

    def list_session_ids(self) -> list[str]:
        return sorted(path.stem for path in self.sessions_dir.glob("*.json"))

    def _load(self, session_id: str) -> HarborSessionRecord:
        safe_session_id = _safe_identifier(session_id)
        path = self.sessions_dir / f"{safe_session_id}.json"
        if not path.exists():
            raise KeyError(f"unknown session: {safe_session_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return HarborSessionRecord(
            session_id=str(data["session_id"]),
            command=str(data["command"]),
            remote_dir=str(data["remote_dir"]),
            remote_input_path=str(data["remote_input_path"]),
            remote_screen_path=str(data["remote_screen_path"]),
            remote_pid_path=str(data.get("remote_pid_path") or f"{data['remote_dir']}/session.pid"),
            registry_path=str(data["registry_path"]),
        )


def _translate_session_keys(keys: str) -> str:
    mapping = {
        "Enter": "\n",
        "Return": "\n",
        "Tab": "\t",
        "Space": " ",
        "C-c": "\x03",
    }
    return mapping.get(keys, keys)


def _safe_identifier(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value).strip())
    cleaned = cleaned.strip("-_")
    if not cleaned:
        raise ValueError("identifier must contain at least one alphanumeric character")
    return cleaned[:80]


def probe_harbor_workspace(
    environment: "BaseEnvironment | Any",
    *,
    candidates: Iterable[str] = DEFAULT_HARBOR_WORKSPACE_PROBE_CANDIDATES,
    timeout_sec: int = DEFAULT_PROBE_TIMEOUT_SEC,
) -> HarborWorkspaceProbe:
    """Discover the remote Harbor workspace root through truthful probes."""

    candidate_list = tuple(str(item).strip() for item in candidates if str(item).strip())
    pwd = _run_remote_text(environment, "pwd", timeout_sec=timeout_sec).strip()
    git_root = _run_remote_text(
        environment,
        "git rev-parse --show-toplevel 2>/dev/null || true",
        cwd=pwd or None,
        timeout_sec=timeout_sec,
    ).strip() or None

    existing: list[str] = []
    for candidate in candidate_list:
        outcome = _run_remote_text(
            environment,
            f"if test -d {candidate}; then printf present; else printf missing; fi",
            timeout_sec=timeout_sec,
        ).strip()
        if outcome == "present":
            existing.append(candidate)

    workspace_root = _choose_workspace_root(
        pwd=pwd,
        git_root=git_root,
        existing_candidates=tuple(existing),
    )
    return HarborWorkspaceProbe(
        pwd=pwd,
        git_root=git_root,
        workspace_root=workspace_root,
        existing_candidates=tuple(existing),
    )


class HarborExecutor(ContainerExecutor):
    """ContainerExecutor-compatible facade backed by a Harbor BaseEnvironment."""

    def __init__(
        self,
        *,
        environment: "BaseEnvironment | Any",
        remote_workspace_root: str,
        local_mirror_root: str | Path,
        scratch_root: str | Path | None = None,
        default_env: dict[str, str] | None = None,
        mirror_excludes: Iterable[str] = DEFAULT_HARBOR_MIRROR_EXCLUDES,
        sync_on_init: bool = True,
    ) -> None:
        backend = ContainerBackend(
            kind="harbor",
            container_workspace_root=str(remote_workspace_root),
        )
        super().__init__(workspace_root=local_mirror_root, backend=backend)
        self.environment = environment
        self.remote_workspace_root = self.container_workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.scratch_root = Path(scratch_root or (self.workspace_root.parent / f"{self.workspace_root.name}_scratch")).resolve(strict=False)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self.default_env = dict(default_env or {})
        self.mirror_excludes = tuple(str(item) for item in mirror_excludes)
        if sync_on_init:
            self.prepare_snapshot()

    def prepare_snapshot(self) -> None:
        """Refresh the local evidence mirror from Harbor without changing authority."""

        self.workspace_root.mkdir(parents=True, exist_ok=True)
        _clear_directory(self.workspace_root)
        remote_archive = f"/tmp/aether2_harbor_snapshot_{uuid.uuid4().hex}.tar"
        local_archive = self._scratch_path("snapshot", suffix=".tar")
        exclude_args = " ".join(
            f"--exclude={shlex.quote(pattern)}" for pattern in self.mirror_excludes
        )
        try:
            _run_awaitable_sync(
                self.environment.exec(
                    command=(
                        f"tar -cf {shlex.quote(remote_archive)} {exclude_args} "
                        f"-C {shlex.quote(self.remote_workspace_root)} ."
                    ),
                    cwd=self.remote_workspace_root,
                    env=self.default_env or None,
                    timeout_sec=120,
                )
            )
            _run_awaitable_sync(self.environment.download_file(remote_archive, local_archive))
            with tarfile.open(local_archive) as archive:
                archive.extractall(self.workspace_root)
        finally:
            local_archive.unlink(missing_ok=True)
            _run_awaitable_sync(
                self.environment.exec(
                    command=f"rm -f {shlex.quote(remote_archive)}",
                    cwd=self.remote_workspace_root,
                    env=self.default_env or None,
                    timeout_sec=30,
                )
            )

    _FILE_NOT_FOUND_MARKERS = (
        "could not find the file",
        "no such file",
        "does not exist",
        "not found",
    )

    def read_text_file(self, path: str | Path) -> str:
        resolved = self.resolve_workspace_path(path)
        remote_path = self.to_container_path(resolved)
        temp_path = self._scratch_path("download", suffix=resolved.suffix or ".txt")
        try:
            _run_awaitable_sync(self.environment.download_file(remote_path, temp_path))
            return temp_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            msg = str(exc).lower()
            if any(marker in msg for marker in self._FILE_NOT_FOUND_MARKERS):
                raise FileNotFoundError(str(resolved)) from exc
            raise
        finally:
            temp_path.unlink(missing_ok=True)

    def write_text_file(self, path: str | Path, content: str) -> None:
        resolved = self.resolve_workspace_path(path)
        remote_path = self.to_container_path(resolved)
        temp_path = self._scratch_path("upload", suffix=resolved.suffix or ".txt")
        try:
            temp_path.write_text(content, encoding="utf-8")
            _run_awaitable_sync(self.environment.upload_file(temp_path, remote_path))
        finally:
            temp_path.unlink(missing_ok=True)

    def create_session_registry(self, state_dir: Path) -> HarborSessionRegistry:
        return HarborSessionRegistry(
            state_dir,
            environment=self.environment,
            remote_workspace_root=self.remote_workspace_root,
            default_env=self.default_env,
        )

    def run(self, cmd: str, timeout_sec: int, cwd: str | Path | None = None) -> RawResult:
        command = str(cmd)
        started_at = perf_counter()
        cwd_path, container_cwd, cwd_error = self._resolve_cwd(cwd)
        if cwd_error is not None:
            return self._finish_error(
                command=command,
                cwd_path=cwd_path,
                started_at=started_at,
                exit_code=2,
                timed_out=False,
                boundary_violation=cwd_error.kind == "workspace_boundary_violation",
                error=cwd_error,
            )

        blocked_token = self._find_boundary_violation(command, cwd_path)
        if blocked_token is not None:
            return self._finish_error(
                command=command,
                cwd_path=cwd_path,
                started_at=started_at,
                exit_code=126,
                timed_out=False,
                boundary_violation=True,
                error=ErrorInfo(
                    kind="workspace_boundary_violation",
                    message=(
                        "workspace_boundary_violation: blocked path-like token outside the task workspace: "
                        f"{self._format_blocked_token(blocked_token)}"
                    ),
                    reason_code="workspace_boundary_violation",
                    failure_class="path_visibility",
                    details="heuristic workspace-root boundary guard",
                    tool_name="run_command",
                    command=command,
                ),
            )

        try:
            completed = _run_awaitable_sync(
                self.environment.exec(
                    command=command,
                    cwd=container_cwd,
                    env=self.default_env or None,
                    timeout_sec=timeout_sec,
                )
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = self._normalize_timeout_streams(exc)
            return RawResult(
                tool="run_command",
                command=command,
                exit_code=124,
                duration_sec=perf_counter() - started_at,
                cwd=str(cwd_path),
                stdout=stdout,
                stderr=stderr,
                workspace_root=str(self.workspace_root),
                timed_out=True,
                boundary_violation=False,
                error=ErrorInfo(
                    kind="timeout",
                    message=f"command timed out after {timeout_sec}s",
                    reason_code="timeout",
                    failure_class="timeout",
                    details="Harbor exec exceeded the configured timeout",
                    tool_name="run_command",
                    command=command,
                    exit_code=124,
                    timed_out=True,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a truthful tool failure
            return self._finish_error(
                command=command,
                cwd_path=cwd_path,
                started_at=started_at,
                exit_code=71,
                timed_out=False,
                boundary_violation=False,
                error=ErrorInfo(
                    kind="harbor_exec_failed",
                    message=str(exc),
                    reason_code="harbor_exec_failed",
                    failure_class="runtime",
                    details="Harbor BaseEnvironment.exec raised an exception",
                    tool_name="run_command",
                    command=command,
                ),
            )

        exit_code = int(getattr(completed, "return_code", 0))
        stdout = getattr(completed, "stdout", "") or ""
        stderr = getattr(completed, "stderr", "") or ""
        error = None
        if exit_code != 0:
            error = ErrorInfo(
                kind="nonzero_exit",
                message=f"command exited with status {exit_code}",
                reason_code="nonzero_exit",
                failure_class="process_exit",
                details="Harbor foreground command completed but returned a non-zero exit code",
                tool_name="run_command",
                command=command,
                exit_code=exit_code,
                timed_out=False,
            )
        return RawResult(
            tool="run_command",
            command=command,
            exit_code=exit_code,
            duration_sec=perf_counter() - started_at,
            cwd=str(cwd_path),
            stdout=str(stdout),
            stderr=str(stderr),
            workspace_root=str(self.workspace_root),
            timed_out=False,
            boundary_violation=False,
            error=error,
        )

    def start_background_job(self, cmd: str, job_id: str | None = None, cwd: str | Path | None = None) -> HarborJobStatus:
        command = str(cmd).strip()
        if not command:
            raise ValueError("empty_command: start_job requires a non-empty command")
        resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        cwd_path, remote_cwd, cwd_error = self._resolve_cwd(cwd)
        if cwd_error is not None:
            raise ValueError(cwd_error.message)

        remote_job_dir = f"{self.remote_workspace_root}/.aether2/harbor_jobs/{resolved_job_id}"
        remote_command_path = f"{remote_job_dir}/command.sh"
        remote_wrapper_path = f"{remote_job_dir}/run.sh"
        remote_log_path = f"{remote_job_dir}/job.log"
        remote_exit_code_path = f"{remote_job_dir}/exit_code"
        remote_pid_path = f"{remote_job_dir}/job.pid"
        remote_meta_path = f"{remote_job_dir}/meta.json"

        _run_awaitable_sync(
            self.environment.exec(
                command=f"mkdir -p {shlex.quote(remote_job_dir)}",
                cwd=remote_cwd,
                env=self.default_env or None,
                timeout_sec=30,
            )
        )
        command_local = self._scratch_path("job", suffix=".sh")
        wrapper_local = self._scratch_path("job", suffix=".sh")
        meta_local = self._scratch_path("job", suffix=".json")
        try:
            command_local.write_text(command + "\n", encoding="utf-8")
            wrapper_local.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env sh",
                        "set +e",
                        f"cd {shlex.quote(remote_cwd)}",
                        (
                            "trap 'status=$?; "
                            f"if [ ! -f {shlex.quote(remote_exit_code_path)} ]; then "
                            f"printf \"%s\\\\n\" \"$status\" > {shlex.quote(remote_exit_code_path)}; "
                            "fi' EXIT"
                        ),
                        f". {shlex.quote(remote_command_path)} >> {shlex.quote(remote_log_path)} 2>&1",
                        "code=$?",
                        f"printf \"%s\\n\" \"$code\" > {shlex.quote(remote_exit_code_path)}",
                        "exit \"$code\"",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            meta_local.write_text(
                json.dumps(
                    {
                        "job_id": resolved_job_id,
                        "cmd": command,
                        "cwd": str(cwd_path),
                        "remote_cwd": remote_cwd,
                        "log_path": remote_log_path,
                        "registry_path": remote_meta_path,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            _run_awaitable_sync(self.environment.upload_file(command_local, remote_command_path))
            _run_awaitable_sync(self.environment.upload_file(wrapper_local, remote_wrapper_path))
            _run_awaitable_sync(self.environment.upload_file(meta_local, remote_meta_path))
        finally:
            command_local.unlink(missing_ok=True)
            wrapper_local.unlink(missing_ok=True)
            meta_local.unlink(missing_ok=True)

        launch = _run_awaitable_sync(
            self.environment.exec(
                command=(
                    f"chmod +x {shlex.quote(remote_command_path)} {shlex.quote(remote_wrapper_path)} && "
                    f"(nohup sh {shlex.quote(remote_wrapper_path)} >/dev/null 2>&1 < /dev/null & echo $! > {shlex.quote(remote_pid_path)}) && "
                    f"cat {shlex.quote(remote_pid_path)}"
                ),
                cwd=remote_cwd,
                env=self.default_env or None,
                timeout_sec=30,
            )
        )
        stdout = str(getattr(launch, "stdout", "") or "").strip().splitlines()
        pid_text = stdout[-1] if stdout else ""
        try:
            pid = int(pid_text)
        except ValueError as exc:
            raise RuntimeError(f"failed to parse Harbor background job pid from output: {pid_text!r}") from exc
        return self.status_background_job(resolved_job_id)

    def status_background_job(self, job_id: str) -> HarborJobStatus:
        safe_job_id = str(job_id).strip()
        if not safe_job_id:
            raise ValueError("empty_job_id")
        remote_job_dir = f"{self.remote_workspace_root}/.aether2/harbor_jobs/{safe_job_id}"
        remote_pid_path = f"{remote_job_dir}/job.pid"
        remote_exit_code_path = f"{remote_job_dir}/exit_code"
        remote_log_path = f"{remote_job_dir}/job.log"
        remote_meta_path = f"{remote_job_dir}/meta.json"
        status_script = (
            f"pid=$(cat {shlex.quote(remote_pid_path)} 2>/dev/null || true); "
            "if [ -z \"$pid\" ]; then echo '__MISSING_PID__'; exit 2; fi; "
            f"code=$(cat {shlex.quote(remote_exit_code_path)} 2>/dev/null || true); "
            "alive=false; if kill -0 \"$pid\" 2>/dev/null; then alive=true; fi; "
            "printf '__PID__%s\\n' \"$pid\"; "
            "printf '__ALIVE__%s\\n' \"$alive\"; "
            "printf '__EXIT__%s\\n' \"$code\"; "
            f"printf '__TAIL__\\n'; tail -c 2048 {shlex.quote(remote_log_path)} 2>/dev/null || true"
        )
        result = _run_awaitable_sync(
            self.environment.exec(
                command=status_script,
                cwd=self.remote_workspace_root,
                env=self.default_env or None,
                timeout_sec=30,
            )
        )
        stdout = str(getattr(result, "stdout", "") or "")
        if "__MISSING_PID__" in stdout:
            raise KeyError(f"unknown job: {safe_job_id}")
        pid = _parse_status_int(stdout, "__PID__")
        exit_code = _parse_status_int(stdout, "__EXIT__", allow_empty=True)
        alive_text = _parse_status_text(stdout, "__ALIVE__")
        tail = stdout.split("__TAIL__\n", 1)[1] if "__TAIL__\n" in stdout else ""
        return HarborJobStatus(
            job_id=safe_job_id,
            pid=pid,
            alive=alive_text == "true" and exit_code is None,
            exit_code=exit_code,
            cwd=self.remote_workspace_root,
            log_path=remote_log_path,
            tail=tail[-2048:],
            registry_path=remote_meta_path,
        )

    def _scratch_path(self, prefix: str, *, suffix: str = "") -> Path:
        bucket = self.scratch_root / prefix
        bucket.mkdir(parents=True, exist_ok=True)
        return bucket / f"{uuid.uuid4().hex}{suffix}"


def run_aether2_harbor_agent(
    *,
    agent: Any,
    instruction: str,
    environment: "BaseEnvironment | Any",
    context: Any,
    adaptive_profile: str,
    adaptive_profile_enabled: bool,
    logs_dir: Path,
    model_env_status: dict[str, Any] | None = None,
    receipt_driven_variant_enabled: bool = False,
) -> dict[str, Any]:
    """Adapter-discoverable Harbor backend entrypoint."""

    workspace_probe = probe_harbor_workspace(environment)
    model_client = _build_backend_model_client()
    preflight = _preflight_model_client(model_client)
    base_record = {
        "adapter": getattr(agent, "name", lambda: "aether2-harbor")(),
        "adaptive_profile": adaptive_profile,
        "adaptive_profile_enabled": bool(adaptive_profile_enabled),
        "receipt_driven_variant_enabled": bool(receipt_driven_variant_enabled),
        "remote_workspace_root": workspace_probe.workspace_root,
        "workspace_probe": asdict(workspace_probe),
        "model_env_status": dict(model_env_status or {}),
    }
    if getattr(model_client, "__class__", type(model_client)).__name__ == "_MissingModelClient":
        record = {
            **base_record,
            "status": "blocked",
            "reason_code": "model_client_unavailable",
            "summary": "Harbor backend is wired, but no production model route could be constructed from the environment.",
        }
        _attach_context_status(context, record)
        return record
    if not preflight["ready"]:
        record = {
            **base_record,
            "status": "blocked",
            "reason_code": "model_client_preflight_failed",
            "summary": "Harbor backend found a model client route, but the local model stack is not ready for a truthful Harbor run.",
            "model_client_preflight": preflight,
        }
        _attach_context_status(context, record)
        return record

    runtime_root = Path(logs_dir).resolve() / "aether2_harbor_runtime"
    host_state_root = runtime_root / "host_state"
    artifacts_dir = runtime_root / "artifacts"
    mirror_root = Path(logs_dir).resolve() / "tmp" / "harbor_workspace_mirror"
    scratch_root = Path(logs_dir).resolve() / "tmp" / "harbor_workspace_staging"
    host_state_root.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    executor = HarborExecutor(
        environment=environment,
        remote_workspace_root=workspace_probe.workspace_root,
        local_mirror_root=mirror_root,
        scratch_root=scratch_root,
    )
    task = TaskSpec(
        task_id=_derive_task_id(environment, context, runtime_root),
        instruction=instruction,
        task_dir=runtime_root,
        workspace_root=host_state_root,
        artifacts_dir=artifacts_dir,
    )
    from harness.aether2.control.loop import run_aether2_loop

    try:
        result = run_aether2_loop(
            task,
            model_client,
            executor,
            deadline_ts=_derive_deadline_ts(context),
            adaptive_profile_enabled=adaptive_profile_enabled,
            receipt_driven_variant_enabled=receipt_driven_variant_enabled,
        )
    except AgentInitializationFailure as exc:
        reason_code = getattr(exc, "reason_code", "agent_initialization_failure")
        record = {
            **base_record,
            "status": "invalid",
            "reason_code": reason_code,
            "summary": str(exc),
            "primary_failure_class": "AGENT_INITIALIZATION",
            "model_calls": 0,
            "steps": 0,
            "artifacts_dir": str(artifacts_dir),
            "host_state_root": str(host_state_root),
            "local_mirror_root": str(mirror_root),
        }
        failure_path = artifacts_dir / "agent_initialization_failure.json"
        failure_path.parent.mkdir(parents=True, exist_ok=True)
        failure_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record["failure_artifact"] = str(failure_path)
        _attach_context_status(context, record)
        return record
    telemetry = {
        "tokens_cached": result.tokens_cached,
        "tokens_fresh": result.tokens_fresh,
        "latency_sec": round(float(result.wall_time), 3),
        "no_progress_streak": int(result.no_delta_streaks),
        "proof_state_delta": None if result.proof_state is None else result.proof_state.get("delta"),
        "proof_state": None if result.proof_state is None else result.proof_state.get("state"),
        "rejected_proxy_evidence": None if result.proof_state is None else list(result.proof_state.get("rejected_proxy_evidence", []) or []),
        "cost_usd": round(float(result.cost), 6),
    }
    primary_failure_class = classify_failure(
        {"mean": result.grader_reward},
        {
            "summary": result.summary,
            "reason_code": "harbor_loop_finished",
            "transcript_repairs": result.transcript_repairs,
            "verifier_readiness": result.verifier_readiness,
        },
        [],
    )
    run_decision_path = _write_run_decision(
        artifacts_dir=artifacts_dir,
        task_id=task.task_id,
        adaptive_profile="on" if adaptive_profile_enabled else "off",
        summary=result.summary,
        finalize_reason=result.finalize_reason,
        verifier_readiness=result.verifier_readiness,
        grader_reward=result.grader_reward,
        telemetry=telemetry,
        primary_failure_class=primary_failure_class,
    )
    record = {
        **base_record,
        "status": "complete",
        "reason_code": "harbor_loop_finished",
        "summary": result.summary,
        "finalize_reason": result.finalize_reason,
        "verifier_readiness": result.verifier_readiness,
        "grader_reward": result.grader_reward,
        "steps": result.steps,
        "model_calls": result.model_calls,
        "tokens_cached": result.tokens_cached,
        "tokens_fresh": result.tokens_fresh,
        "cost": result.cost,
        "transcript_repairs": result.transcript_repairs,
        "wall_time": result.wall_time,
        "telemetry": telemetry,
        "primary_failure_class": primary_failure_class,
        "run_decision_path": str(run_decision_path),
        "artifacts_dir": str(artifacts_dir),
        "host_state_root": str(host_state_root),
        "local_mirror_root": str(mirror_root),
    }
    _attach_context_status(context, record)
    return record


def _write_run_decision(
    *,
    artifacts_dir: Path,
    task_id: str,
    adaptive_profile: str,
    summary: str,
    finalize_reason: str,
    verifier_readiness: bool,
    grader_reward: float | None,
    telemetry: dict[str, Any],
    primary_failure_class: str,
) -> Path:
    path = artifacts_dir / "RUN_DECISION.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _build_run_decision_markdown(
            task_id=task_id,
            adaptive_profile=adaptive_profile,
            summary=summary,
            finalize_reason=finalize_reason,
            verifier_readiness=verifier_readiness,
            grader_reward=grader_reward,
            telemetry=telemetry,
            primary_failure_class=primary_failure_class,
        ),
        encoding="utf-8",
    )
    return path


def _build_run_decision_markdown(
    *,
    task_id: str,
    adaptive_profile: str,
    summary: str,
    finalize_reason: str,
    verifier_readiness: bool,
    grader_reward: float | None,
    telemetry: dict[str, Any],
    primary_failure_class: str,
) -> str:
    reward_text = "n/a" if grader_reward is None else str(grader_reward)
    telemetry_lines = [
        f"- tokens_cached: {telemetry.get('tokens_cached')}",
        f"- tokens_fresh: {telemetry.get('tokens_fresh')}",
        f"- latency_sec: {telemetry.get('latency_sec')}",
        f"- no_progress_streak: {telemetry.get('no_progress_streak')}",
        f"- proof_state: {telemetry.get('proof_state')}",
        f"- proof_state_delta: {telemetry.get('proof_state_delta')}",
        f"- rejected_proxy_evidence: {telemetry.get('rejected_proxy_evidence')}",
        f"- cost_usd: {telemetry.get('cost_usd')}",
    ]
    terminal_lines = "\n".join(f"- {name}" for name in TERMINAL_MODEL_LIMITED_CLASSES)
    return (
        "# RUN_DECISION\n\n"
        f"- task_id: {task_id}\n"
        f"- adaptive_profile: {adaptive_profile}\n"
        f"- finalize_reason: {finalize_reason}\n"
        f"- verifier_readiness: {str(bool(verifier_readiness)).lower()}\n"
        f"- grader_reward: {reward_text}\n"
        f"- primary_failure_class: {primary_failure_class}\n\n"
        "## Summary\n\n"
        f"{summary.strip() or 'No summary recorded.'}\n\n"
        "## Telemetry\n\n"
        + "\n".join(telemetry_lines)
        + "\n\n## Mechanism Admission Rule\n\n"
        "Add a new harness mechanism only when the smallest responsible layer is harness-responsible.\n"
        "If the terminal class is model-limited, record it honestly and do not scaffold around it by default.\n\n"
        "## Terminal Model-Limited Classes\n\n"
        + terminal_lines
        + "\n"
    )


def _choose_workspace_root(
    *,
    pwd: str,
    git_root: str | None,
    existing_candidates: tuple[str, ...],
) -> str:
    if git_root:
        return git_root
    for candidate in existing_candidates:
        if pwd == candidate or pwd.startswith(f"{candidate}/"):
            return candidate
    if existing_candidates:
        return existing_candidates[0]
    return pwd or DEFAULT_HARBOR_WORKSPACE_PROBE_CANDIDATES[0]


def _run_remote_text(
    environment: "BaseEnvironment | Any",
    command: str,
    *,
    cwd: str | None = None,
    timeout_sec: int = DEFAULT_PROBE_TIMEOUT_SEC,
) -> str:
    result = _run_awaitable_sync(environment.exec(command=command, cwd=cwd, env=None, timeout_sec=timeout_sec))
    return str(getattr(result, "stdout", "") or "")


def _run_awaitable_sync(awaitable: Awaitable[_T]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, _T] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001 - reraised on caller thread
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result["value"]


def _parse_status_text(stdout: str, marker: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(marker):
            return line.removeprefix(marker).strip()
    return ""


def _parse_status_int(stdout: str, marker: str, *, allow_empty: bool = False) -> int | None:
    text = _parse_status_text(stdout, marker)
    if allow_empty and not text:
        return None
    return int(text)


def _clear_directory(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _build_backend_model_client() -> Any:
    from harness.aether2.runtime.bridge_harbor import _build_model_client

    return _build_model_client()


def _preflight_model_client(model_client: Any) -> dict[str, Any]:
    issues: list[str] = []
    if importlib.util.find_spec("litellm") is None:
        issues.append("missing python dependency: litellm")
    if importlib.util.find_spec("tenacity") is None:
        issues.append("missing python dependency: tenacity")

    route = getattr(model_client, "model_route", None)
    if isinstance(route, dict):
        settings = route.get("request_settings")
        settings_dict = dict(settings) if isinstance(settings, dict) else {}
        pricing_model_id = str(settings_dict.get("pricing_model_id") or "").strip().lower()
        temperature = settings_dict.get("temperature")
        if pricing_model_id.startswith("gpt-5") and temperature not in (None, 1, 1.0):
            issues.append(
                f"route requests temperature={temperature!r}, but the current gpt-5 client path requires temperature=1 or unset"
            )

    return {
        "ready": not issues,
        "issues": issues,
    }


def _derive_task_id(environment: Any, context: Any, runtime_root: Path) -> str:
    for value in (
        getattr(context, "task_id", None),
        getattr(context, "run_id", None),
        getattr(environment, "session_id", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return runtime_root.name


def _derive_deadline_ts(context: Any) -> float:
    metadata = getattr(context, "metadata", None)
    for value in (
        getattr(context, "deadline_ts", None),
        getattr(context, "deadline", None),
        metadata.get("deadline_ts") if isinstance(metadata, dict) else None,
    ):
        if isinstance(value, (int, float)) and float(value) > time.time():
            return float(value)
    return time.time() + 3600.0


def _attach_context_status(context: Any, record: dict[str, Any]) -> None:
    metadata = dict(getattr(context, "metadata", None) or {})
    metadata["aether2_harbor"] = record
    context.metadata = metadata


__all__ = [
    "DEFAULT_HARBOR_WORKSPACE_PROBE_CANDIDATES",
    "DEFAULT_PROBE_TIMEOUT_SEC",
    "HarborExecutor",
    "HarborWorkspaceProbe",
    "probe_harbor_workspace",
    "run_aether2_harbor_agent",
]
