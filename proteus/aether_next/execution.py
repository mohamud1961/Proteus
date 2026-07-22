from __future__ import annotations

from dataclasses import dataclass, replace
import fnmatch
import hashlib
import re
from typing import Any, Callable, Mapping, Protocol

from .ledger import Receipt
from .runtime_ir import ActionRequest, EnvMap, normalize_relpath


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    modified_paths: tuple[str, ...] = ()
    produced_artifacts: tuple[str, ...] = ()
    removed_paths: tuple[str, ...] = ()
    state_delta: Mapping[str, Any] = None  # type: ignore[assignment]
    metrics: Mapping[str, float] = None  # type: ignore[assignment]
    provenance: tuple[str, ...] = ()
    # Truthful capture beyond the inline cap: when a stream exceeds the
    # inline retention bound, the FULL stream is spooled to this path and the
    # inline field holds a marked head+tail.  Empty string = inline is full.
    stdout_overflow_path: str = ""
    stderr_overflow_path: str = ""
    stdout_bytes_total: int = -1
    stderr_bytes_total: int = -1
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_delta", dict(self.state_delta or {}))
        object.__setattr__(self, "metrics", dict(self.metrics or {}))
        if self.stdout_bytes_total < 0:
            object.__setattr__(self, "stdout_bytes_total", len(self.stdout))
        if self.stderr_bytes_total < 0:
            object.__setattr__(self, "stderr_bytes_total", len(self.stderr))


@dataclass(frozen=True)
class ProcessHandle:
    process_id: str
    name: str
    command: str
    interactive: bool = False
    live: bool = True
    endpoint: str = ""
    detail: str = ""
    pid: int | None = None
    start_time_ticks: str = ""
    command_sha256: str = ""
    process_generation: str = ""
    stdout_log: str = ""
    stderr_log: str = ""

@dataclass(frozen=True)
class ProbeResult:
    target: str
    live: bool
    detail: str = ""
    fresh: bool = True
    service_name: str = ""
    process_id: str = ""
    process_generation: str = ""
    process_generation_verified: bool = False
    endpoint_owner_pids: tuple[int, ...] = ()

@dataclass(frozen=True)
class ArtifactInspection:
    path: str
    mode: str
    success: bool
    extracted_text: str = ""
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


class Executor(Protocol):
    def read_file(self, path: str) -> str:
        ...

    def write_file(self, path: str, content: str) -> None:
        ...

    def run_command(self, command: str, *, cwd: str | None = None, timeout_s: int = 30) -> CommandResult:
        ...

    def launch_process(
        self,
        name: str,
        command: str,
        *,
        interactive: bool = False,
        cwd: str | None = None,
    ) -> ProcessHandle:
        ...

    def probe_process(self, target: str) -> ProbeResult:
        ...

    def stop_process(self, target: str) -> bool:
        ...

    def inspect_artifact(self, path: str, mode: str) -> ArtifactInspection:
        ...

    def refresh_envmap(self, envmap: EnvMap) -> EnvMap:
        ...

    def exists(self, path: str) -> bool:
        ...

    def glob(self, pattern: str) -> tuple[str, ...]:
        ...


class BootstrapEngine:
    _TEMPLATES: dict[str, Callable[[Mapping[str, Any]], str]] = {
        "apt": lambda args: f"apt-get update && apt-get install -y {args['target']}",
        "pip": lambda args: f"pip install {args['target']}",
        "uv": lambda args: f"uv pip install {args['target']}",
        "npm": lambda args: f"npm install {args['target']}",
        "cargo": lambda args: f"cargo install {args['target']}",
        "opam": lambda args: f"opam install -y {args['target']}",
        "git": lambda args: f"git clone {args['source']}",
        "wget": lambda args: f"wget -O {args.get('output', 'download.bin')} {args['source']}",
        "curl": lambda args: f"curl -L {args['source']} -o {args.get('output', 'download.bin')}",
        "hf": lambda args: f"hf download {args['target']}",
    }

    def execute(
        self,
        action: ActionRequest,
        step: int,
        executor: Executor,
        envmap: EnvMap,
    ) -> tuple[Receipt, EnvMap | None]:
        manager = str(action.arguments.get("manager", "")).strip()
        builder = self._TEMPLATES.get(manager)
        if builder is None:
            receipt = Receipt(
                receipt_id=f"step-{step}:{action.action_id}:bootstrap",
                step=step,
                kind="bootstrap",
                success=False,
                summary=f"unsupported bootstrap manager: {manager}",
                failure_class="bootstrap_required",
                payload={
                    "manager": manager,
                    "candidate_id": action.candidate_id,
                },
            )
            return receipt, None

        try:
            command = builder(action.arguments)
        except KeyError as exc:
            receipt = Receipt(
                receipt_id=f"step-{step}:{action.action_id}:bootstrap",
                step=step,
                kind="bootstrap",
                success=False,
                summary=f"bootstrap arguments missing: {exc.args[0]}",
                failure_class="bootstrap_required",
                payload={
                    "manager": manager,
                    "candidate_id": action.candidate_id,
                },
            )
            return receipt, None

        result = executor.run_command(command, cwd=envmap.workspace_root)
        refreshed = executor.refresh_envmap(envmap) if result.success else envmap
        old_caps = set(envmap.capabilities.keys())
        new_caps = set(refreshed.capabilities.keys())
        added_caps = tuple(sorted(new_caps - old_caps))
        receipt = Receipt(
            receipt_id=f"step-{step}:{action.action_id}:bootstrap",
            step=step,
            kind="bootstrap",
            success=result.success,
            summary=f"bootstrap {manager}: exit={result.exit_code}",
            state_change=result.success,
            failure_class="" if result.success else "bootstrap_required",
            payload={
                "manager": manager,
                "command": command,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "modified_paths": tuple(normalize_relpath(path, envmap.workspace_root) for path in result.modified_paths),
                "artifact_paths": tuple(normalize_relpath(path, envmap.workspace_root) for path in result.produced_artifacts),
                "provenance": list(result.provenance),
                "capabilities_added": added_caps,
                "candidate_id": action.candidate_id,
            },
        )
        if refreshed.digest() != envmap.digest():
            return receipt, refreshed
        return receipt, None


class ProcessOrchestratorV2:
    def launch(
        self,
        action: ActionRequest,
        step: int,
        executor: Executor,
        *,
        workspace_root: str,
        interactive: bool,
    ) -> Receipt:
        service_name = str(action.arguments.get("service_name", "")).strip()
        command = str(action.arguments.get("command", "")).strip()
        handle = executor.launch_process(
            service_name,
            command,
            interactive=interactive,
            cwd=workspace_root,
        )
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:launch",
            step=step,
            kind="process_launch",
            success=handle.live,
            summary=f"launched process {handle.name}",
            state_change=True,
            failure_class="" if handle.live else "service_not_ready",
            payload={
                "process_id": handle.process_id,
                "service_name": handle.name,
                "command": handle.command,
                "interactive": handle.interactive,
                "live": handle.live,
                "detail": handle.detail,
                "pid": handle.pid,
                "start_time_ticks": handle.start_time_ticks,
                "command_sha256": handle.command_sha256,
                "process_generation": handle.process_generation,
                "stdout_log": handle.stdout_log,
                "stderr_log": handle.stderr_log,
                "candidate_id": action.candidate_id,
            },
        )

    def probe(
        self,
        action: ActionRequest,
        step: int,
        executor: Executor,
    ) -> Receipt:
        target = str(action.arguments.get("target", "")).strip()
        probe = executor.probe_process(target)
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:probe",
            step=step,
            kind="service_probe",
            success=probe.live,
            summary=f"probe {target}: {'live' if probe.live else 'not_live'}",
            state_change=False,
            failure_class="" if probe.live else "service_not_ready",
            payload={
                "target": probe.target,
                "service_name": probe.service_name or target,
                "live": probe.live,
                "detail": probe.detail,
                "process_id": probe.process_id,
                "process_generation": probe.process_generation,
                "process_generation_verified": probe.process_generation_verified,
                "endpoint_owner_pids": list(probe.endpoint_owner_pids),
                "candidate_id": action.candidate_id,
            },
        )

    def stop(
        self,
        action: ActionRequest,
        step: int,
        executor: Executor,
    ) -> Receipt:
        target = str(action.arguments.get("target", "")).strip()
        stopped = executor.stop_process(target)
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:stop",
            step=step,
            kind="process_stop",
            success=stopped,
            summary=f"stop {target}: {'ok' if stopped else 'not_found'}",
            state_change=stopped,
            failure_class="" if stopped else "service_not_ready",
            payload={
                "process_id": target,
                "service_name": target,
                "live": False,
                "candidate_id": action.candidate_id,
            },
        )


class PerceptionLane:
    def inspect(
        self,
        action: ActionRequest,
        step: int,
        executor: Executor,
        *,
        workspace_root: str,
    ) -> Receipt:
        path = normalize_relpath(str(action.arguments.get("path", "")), workspace_root)
        mode = str(action.arguments.get("mode", "")).strip()
        inspection = executor.inspect_artifact(path, mode)
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:inspect",
            step=step,
            kind="artifact_inspection",
            success=inspection.success,
            summary=inspection.detail or f"inspect {path}",
            state_change=False,
            failure_class="" if inspection.success else "perception_required",
            payload={
                "path": path,
                "mode": mode,
                "artifact_paths": (path,) if inspection.success else (),
                "extracted_text": inspection.extracted_text,
                "metadata": dict(inspection.metadata),
                "candidate_id": action.candidate_id,
            },
        )


class ExperimentEngine:
    _METRIC_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(-?\d+(?:\.\d+)?)")

    def run(
        self,
        action: ActionRequest,
        step: int,
        executor: Executor,
        *,
        workspace_root: str,
    ) -> Receipt:
        command = str(action.arguments.get("command", "")).strip()
        metric_name = str(action.arguments.get("metric_name", "")).strip()
        result = executor.run_command(command, cwd=workspace_root)

        metric_value = None
        if metric_name and metric_name in result.metrics:
            metric_value = result.metrics[metric_name]
        elif metric_name:
            metric_value = self._parse_metric(metric_name, result.stdout + "\n" + result.stderr)

        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:experiment",
            step=step,
            kind="experiment",
            success=result.success,
            summary=f"experiment {action.candidate_id}: exit={result.exit_code}",
            state_change=result.success,
            failure_class="" if result.success else "command_failure",
            payload={
                "candidate_id": action.arguments.get("candidate_id", action.candidate_id),
                "candidate_summary": action.arguments.get("summary", action.candidate_id),
                "command": command,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "metric_name": metric_name,
                "metric_value": metric_value,
                "modified_paths": tuple(normalize_relpath(path, workspace_root) for path in result.modified_paths),
                "artifact_paths": tuple(normalize_relpath(path, workspace_root) for path in result.produced_artifacts),
            },
        )

    def _parse_metric(self, metric_name: str, text: str) -> float | None:
        for match in self._METRIC_RE.finditer(text):
            if match.group(1) == metric_name:
                try:
                    return float(match.group(2))
                except ValueError:
                    return None
        return None


class MemoryExecutor:
    def __init__(
        self,
        *,
        workspace_root: str = "/app",
        files: Mapping[str, str] | None = None,
        refresh_hook: Callable[[EnvMap, "MemoryExecutor"], EnvMap] | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.files: dict[str, str] = {
            normalize_relpath(path, workspace_root): content
            for path, content in (files or {}).items()
        }
        self.command_handlers: dict[str, Callable[["MemoryExecutor", str], CommandResult]] = {}
        self.launch_handlers: dict[str, Callable[["MemoryExecutor", str, str, bool], ProcessHandle]] = {}
        self.inspect_handlers: dict[str, Callable[["MemoryExecutor", str, str], ArtifactInspection]] = {}
        self.processes: dict[str, ProcessHandle] = {}
        self.command_history: list[str] = []
        self.refresh_hook = refresh_hook

    def register_command(self, command: str, handler: Callable[["MemoryExecutor", str], CommandResult]) -> None:
        self.command_handlers[command] = handler

    def register_launch(
        self,
        key: str,
        handler: Callable[["MemoryExecutor", str, str, bool], ProcessHandle],
    ) -> None:
        self.launch_handlers[key] = handler

    def register_inspector(
        self,
        path: str,
        handler: Callable[["MemoryExecutor", str, str], ArtifactInspection],
    ) -> None:
        self.inspect_handlers[normalize_relpath(path, self.workspace_root)] = handler

    def read_file(self, path: str) -> str:
        normalized = normalize_relpath(path, self.workspace_root)
        if normalized not in self.files:
            raise FileNotFoundError(normalized)
        return self.files[normalized]

    def write_file(self, path: str, content: str) -> None:
        normalized = normalize_relpath(path, self.workspace_root)
        self.files[normalized] = content

    def run_command(self, command: str, *, cwd: str | None = None, timeout_s: int = 30) -> CommandResult:
        del cwd, timeout_s
        self.command_history.append(command)
        handler = self.command_handlers.get(command)
        if handler is None:
            return CommandResult(
                command=command,
                exit_code=127,
                stderr=f"{command}: command not found",
            )
        return handler(self, command)

    def launch_process(
        self,
        name: str,
        command: str,
        *,
        interactive: bool = False,
        cwd: str | None = None,
    ) -> ProcessHandle:
        del cwd
        handler = self.launch_handlers.get(command) or self.launch_handlers.get(name)
        if handler is not None:
            handle = handler(self, name, command, interactive)
        else:
            ordinal = len(self.processes) + 1
            pid = 10_000 + ordinal
            start_ticks = str(ordinal)
            command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
            generation = hashlib.sha256(
                f"memory\0{pid}\0{start_ticks}\0{command_sha256}".encode("utf-8")
            ).hexdigest()[:24]
            process_id = f"process:{generation}"
            handle = ProcessHandle(
                process_id=process_id,
                name=name,
                command=command,
                interactive=interactive,
                live=True,
                endpoint=f"local://{name}",
                detail="started",
                pid=pid,
                start_time_ticks=start_ticks,
                command_sha256=command_sha256,
                process_generation=generation,
            )
        # A new generation supersedes earlier generations with the same name.
        for existing_id, existing in tuple(self.processes.items()):
            if existing.name == handle.name and existing.live:
                self.processes[existing_id] = replace(existing, live=False, detail="superseded")
        self.processes[handle.process_id] = handle
        return handle

    def _registered_process(self, target: str) -> ProcessHandle | None:
        direct = self.processes.get(target)
        if direct is not None:
            return direct
        matches = [handle for handle in self.processes.values() if handle.name == target]
        return matches[-1] if matches else None

    def probe_process(self, target: str) -> ProbeResult:
        handle = self._registered_process(target)
        if handle is None:
            return ProbeResult(target=target, live=False, detail="not found", service_name=target)
        return ProbeResult(
            target=target,
            live=handle.live,
            detail=handle.detail or ("live" if handle.live else "dead"),
            service_name=handle.name,
            process_id=handle.process_id,
            process_generation=handle.process_generation,
            process_generation_verified=bool(handle.live and handle.process_generation),
            endpoint_owner_pids=((handle.pid,) if handle.pid is not None else ()),
        )

    def stop_process(self, target: str) -> bool:
        handle = self._registered_process(target)
        if handle is None:
            return False
        self.processes[handle.process_id] = ProcessHandle(
            process_id=handle.process_id,
            name=handle.name,
            command=handle.command,
            interactive=handle.interactive,
            live=False,
            endpoint=handle.endpoint,
            detail="stopped",
            pid=handle.pid,
            start_time_ticks=handle.start_time_ticks,
            command_sha256=handle.command_sha256,
            process_generation=handle.process_generation,
            stdout_log=handle.stdout_log,
            stderr_log=handle.stderr_log,
        )
        return True

    def inspect_artifact(self, path: str, mode: str) -> ArtifactInspection:
        normalized = normalize_relpath(path, self.workspace_root)
        handler = self.inspect_handlers.get(normalized)
        if handler is not None:
            return handler(self, normalized, mode)
        if normalized not in self.files:
            return ArtifactInspection(
                path=normalized,
                mode=mode,
                success=False,
                detail="missing artifact",
            )
        return ArtifactInspection(
            path=normalized,
            mode=mode,
            success=True,
            extracted_text=self.files[normalized],
            metadata={"length": len(self.files[normalized])},
            detail=f"inspected {normalized}",
        )

    def refresh_envmap(self, envmap: EnvMap) -> EnvMap:
        if self.refresh_hook is None:
            return envmap
        return self.refresh_hook(envmap, self)

    def exists(self, path: str) -> bool:
        normalized = normalize_relpath(path, self.workspace_root)
        return normalized in self.files

    def glob(self, pattern: str) -> tuple[str, ...]:
        normalized = normalize_relpath(pattern, self.workspace_root)
        matches = sorted(path for path in self.files if fnmatch.fnmatch(path, normalized))
        return tuple(matches)
