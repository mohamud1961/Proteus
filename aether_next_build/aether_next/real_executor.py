"""Subprocess-backed Executor for Aether-Next against a real workspace directory."""
from __future__ import annotations

import fnmatch
import os
import platform
import pwd
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .evidence_finalization import export_content_addressed_files
from .execution import (
    ArtifactInspection,
    CommandResult,
    ProcessHandle,
    ProbeResult,
)
from .runtime_ir import EnvMap, CapabilityDescriptor, normalize_relpath


_STDOUT_CAP = 20_000
_STDERR_CAP = 20_000
# Full streams are kept inline up to this bound; beyond it the complete
# stream is spooled to disk and the inline text is a marked head+tail.
# Nothing is ever destroyed.
_INLINE_STREAM_CAP = 1_000_000
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}
_MAX_SCAN_ENTRIES = 5_000


def _resolve_safe(
    workspace_root: str,
    path: str,
    *,
    for_write: bool = False,
) -> str:
    """Resolve one workspace path or fail closed on escape/symlink ambiguity.

    String-prefix checks are unsafe (``/app`` is a prefix of ``/app_evil``).
    Reads resolve the complete path and require it to remain beneath the real
    workspace root. Writes resolve the parent, reject a symlink destination,
    and require the resulting directory entry to remain beneath the root.
    """
    raw = str(path or "").strip()
    if not raw:
        raise PermissionError("empty workspace path")
    root = Path(workspace_root).resolve(strict=True)
    if raw == "/app" or raw.startswith("/app/"):
        norm = normalize_relpath(raw, "/app")
        lexical = root / norm
    elif Path(raw).is_absolute():
        norm = raw
        lexical = Path(raw)
    else:
        norm = normalize_relpath(raw, workspace_root)
        lexical = root / norm
    if lexical == root or norm in {"", "."}:
        candidate = root
    elif for_write:
        parent = lexical.parent.resolve(strict=False)
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise PermissionError(f"path escapes workspace: {raw}") from exc
        candidate = parent / lexical.name
        if candidate.is_symlink():
            raise PermissionError(f"refusing write through symlink: {raw}")
    else:
        candidate = lexical.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace: {raw}") from exc
    return str(candidate)


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n... [truncated at {cap} chars]"


def _decode_partial(raw: Any) -> str:
    """Decode partial output captured by TimeoutExpired (bytes even in text mode)."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


class StreamSpooler:
    """Truthful stream retention: full inline up to a cap, full spool beyond.

    ``finalize(stream_text, tag)`` returns ``(inline_text, overflow_path)``.
    When the stream fits the inline cap, it is returned verbatim with no
    overflow file.  When it exceeds the cap, the COMPLETE stream is written to
    a spool file and the inline text is a clearly marked head+tail excerpt.
    """

    def __init__(self, *, inline_cap: int = _INLINE_STREAM_CAP) -> None:
        self._inline_cap = max(1_000, int(inline_cap))
        self._spool_dir: str | None = None
        self._counter = 0

    def _ensure_dir(self) -> str:
        if self._spool_dir is None:
            import tempfile
            self._spool_dir = tempfile.mkdtemp(prefix="aether_output_spool_")
        return self._spool_dir

    def finalize(self, stream_text: str, tag: str) -> tuple[str, str]:
        if len(stream_text) <= self._inline_cap:
            return stream_text, ""
        directory = self._ensure_dir()
        self._counter += 1
        safe_tag = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in tag)[:60]
        path = os.path.join(directory, f"{self._counter:06d}_{safe_tag}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stream_text)
        half = self._inline_cap // 2
        omitted = len(stream_text) - (half * 2)
        inline = (
            stream_text[:half]
            + f"\n... [omitted {omitted} chars inline; full {len(stream_text)}-char stream spooled to {path}]\n"
            + stream_text[-half:]
        )
        return inline, path

    def overflow_paths(self) -> tuple[str, ...]:
        if self._spool_dir is None or not os.path.isdir(self._spool_dir):
            return ()
        return tuple(
            str(path)
            for path in sorted(Path(self._spool_dir).iterdir())
            if path.is_file()
        )

    def export_to(self, destination: str) -> dict[str, Any]:
        return export_content_addressed_files(self.overflow_paths(), destination)


def _snapshot_mtimes(root: str) -> dict[str, float]:
    """Return {relative_path: mtime} for files under *root*, bounded."""
    result: dict[str, float] = {}
    count = 0
    root_path = Path(root)
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not (Path(dirpath) / d).is_symlink()
            ]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    rel = os.path.relpath(full, root)
                    result[rel] = os.path.getmtime(full)
                except OSError:
                    pass
                count += 1
                if count >= _MAX_SCAN_ENTRIES:
                    return result
    except OSError:
        pass
    return result


class SubprocessExecutor:
    """Real filesystem + subprocess executor within a workspace directory."""

    def __init__(self, workspace_root: str, *, default_timeout_s: int = 120) -> None:
        self._root = str(Path(workspace_root).resolve())
        self._default_timeout_s = max(1, default_timeout_s)
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._process_meta: dict[str, ProcessHandle] = {}
        self._spooler = StreamSpooler()
        os.makedirs(self._root, exist_ok=True)

    def for_workspace(self, workspace_root: str) -> "SubprocessExecutor":
        """Return a new executor constrained to a trusted isolated workspace."""
        return SubprocessExecutor(
            workspace_root,
            default_timeout_s=self._default_timeout_s,
        )

    def export_spools(self, destination: str) -> dict[str, Any]:
        return self._spooler.export_to(destination)

    # ---- Filesystem --------------------------------------------------------

    def read_file(self, path: str) -> str:
        resolved = _resolve_safe(self._root, path)
        if not os.path.isfile(resolved):
            raise FileNotFoundError(resolved)
        with open(resolved, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def read_file_bytes(self, path: str) -> bytes:
        resolved = _resolve_safe(self._root, path)
        if not os.path.isfile(resolved):
            raise FileNotFoundError(resolved)
        with open(resolved, "rb") as fh:
            return fh.read()

    def write_file(self, path: str, content: str) -> None:
        resolved = _resolve_safe(self._root, path, for_write=True)
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        # The workspace is bind-mounted into a container that runs as root; a
        # run_command executed via docker exec can create/overwrite a file at
        # this path as root before this call runs. open(path, "w") then fails
        # with PermissionError even though this process owns the enclosing
        # directory, because overwriting an existing file's contents requires
        # write permission on the FILE (owner/group/other bits), not the
        # directory. Removing the directory entry first only requires write
        # permission on the parent directory, which this process always has
        # (it created the workspace) -- sidesteps the ownership mismatch
        # entirely without needing chmod/chown (which would themselves fail
        # for a file this process doesn't own).
        try:
            os.remove(resolved)
        except FileNotFoundError:
            pass
        with open(resolved, "w", encoding="utf-8") as fh:
            fh.write(content)

    def exists(self, path: str) -> bool:
        resolved = _resolve_safe(self._root, path)
        return os.path.exists(resolved)

    def glob(self, pattern: str) -> tuple[str, ...]:
        root_path = Path(self._root)
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not (Path(dirpath) / d).is_symlink()
            ]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, self._root)
                if fnmatch.fnmatch(rel, pattern):
                    matches.append(rel)
                if len(matches) >= _MAX_SCAN_ENTRIES:
                    break
        return tuple(sorted(matches))

    # ---- Command execution -------------------------------------------------

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: int = 30,
    ) -> CommandResult:
        effective_cwd = _resolve_safe(self._root, cwd or self._root)
        if not os.path.isdir(effective_cwd):
            raise NotADirectoryError(effective_cwd)
        effective_timeout = timeout_s if timeout_s > 0 else self._default_timeout_s

        before = _snapshot_mtimes(self._root)

        timed_out = False
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=effective_cwd,
                capture_output=True,
                text=True, errors="replace",
                timeout=effective_timeout,
            )
            exit_code = proc.returncode
            raw_stdout, raw_stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            # Preserve partial output truthfully; never destroy what ran.
            timed_out = True
            exit_code = 124
            raw_stdout = _decode_partial(exc.stdout)
            raw_stderr = _decode_partial(exc.stderr) + (
                f"\n[harness] command timed out after {effective_timeout}s; "
                "partial output above is preserved"
            )

        stdout_total, stderr_total = len(raw_stdout), len(raw_stderr)
        stdout, stdout_overflow = self._spooler.finalize(raw_stdout, "stdout")
        stderr, stderr_overflow = self._spooler.finalize(raw_stderr, "stderr")

        after = _snapshot_mtimes(self._root)

        modified: list[str] = []
        produced: list[str] = []
        for rel, mtime in after.items():
            if rel not in before:
                produced.append(rel)
            elif before[rel] != mtime:
                modified.append(rel)

        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            modified_paths=tuple(sorted(modified)),
            produced_artifacts=tuple(sorted(produced)),
            metrics={},
            stdout_overflow_path=stdout_overflow,
            stderr_overflow_path=stderr_overflow,
            stdout_bytes_total=stdout_total,
            stderr_bytes_total=stderr_total,
            timed_out=timed_out,
        )

    def run_command_with_virtual_workspace(
        self,
        command: str,
        *,
        virtual_workspace_root: str = "/app",
        timeout_s: int = 30,
    ) -> CommandResult:
        """Run against this workspace through an isolated Linux bind mount.

        Frozen host replays have no native ``/app`` namespace.  Rather than
        pretending that absolute task paths work, use a private root mount
        namespace when the host explicitly supports it.  The mount dies with
        the child process, so neither the original workspace nor the host
        ``/app`` is mutated.  Unsupported hosts fail closed for this route.
        """
        if virtual_workspace_root != "/app" or platform.system() != "Linux":
            return CommandResult(
                command=command, exit_code=126,
                stderr="overlay_virtual_workspace_unavailable: isolated /app mount requires Linux",
            )
        user = pwd.getpwuid(os.getuid()).pw_name
        namespace_script = (
            'set -eu; mkdir -p /app; mount --bind "$1" /app; '
            'exec runuser -u "$2" -- bash -lc "$3"'
        )
        effective_timeout = timeout_s if timeout_s > 0 else self._default_timeout_s
        before = _snapshot_mtimes(self._root)
        timed_out = False
        try:
            proc = subprocess.run(
                [
                    "sudo", "-n", "unshare", "--mount", "--fork", "--propagation", "private",
                    "bash", "-c", namespace_script, "aether-overlay", self._root, user, command,
                ],
                cwd=self._root,
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
                f"\n[harness] virtual-workspace command timed out after {effective_timeout}s"
            )
        if exit_code != 0 and "sudo:" in raw_stderr:
            raw_stderr = "overlay_virtual_workspace_unavailable: " + raw_stderr
        stdout_total, stderr_total = len(raw_stdout), len(raw_stderr)
        stdout, stdout_overflow = self._spooler.finalize(raw_stdout, "stdout")
        stderr, stderr_overflow = self._spooler.finalize(raw_stderr, "stderr")
        after = _snapshot_mtimes(self._root)
        modified = tuple(sorted(rel for rel, mtime in after.items() if rel in before and before[rel] != mtime))
        produced = tuple(sorted(rel for rel in after if rel not in before))
        return CommandResult(
            command=command, exit_code=exit_code, stdout=stdout, stderr=stderr,
            modified_paths=modified, produced_artifacts=produced, metrics={},
            stdout_overflow_path=stdout_overflow, stderr_overflow_path=stderr_overflow,
            stdout_bytes_total=stdout_total, stderr_bytes_total=stderr_total,
            timed_out=timed_out,
        )

    # ---- Process management ------------------------------------------------

    def launch_process(
        self,
        name: str,
        command: str,
        *,
        interactive: bool = False,
        cwd: str | None = None,
    ) -> ProcessHandle:
        effective_cwd = cwd or self._root
        process_id = f"proc-{uuid.uuid4().hex[:8]}"

        try:
            proc = subprocess.Popen(
                ["bash", "-lc", command],
                cwd=effective_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return ProcessHandle(
                process_id=process_id,
                name=name,
                command=command,
                interactive=interactive,
                live=False,
                detail=f"launch failed: {exc}",
            )

        self._processes[process_id] = proc
        handle = ProcessHandle(
            process_id=process_id,
            name=name,
            command=command,
            interactive=interactive,
            live=True,
            endpoint=f"pid://{proc.pid}",
            detail=f"started pid={proc.pid}",
        )
        self._process_meta[process_id] = handle
        return handle

    def probe_process(self, target: str) -> ProbeResult:
        proc, meta = self._find_process(target)
        if proc is None:
            return ProbeResult(
                target=target,
                live=False,
                detail="not found",
                service_name=target,
            )
        alive = proc.poll() is None
        return ProbeResult(
            target=target,
            live=alive,
            detail=f"pid={proc.pid} {'alive' if alive else 'exited'}",
            service_name=meta.name if meta else target,
        )

    def stop_process(self, target: str) -> bool:
        proc, _meta = self._find_process(target)
        if proc is None:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        return True

    def _find_process(
        self, target: str
    ) -> tuple[subprocess.Popen[str] | None, ProcessHandle | None]:
        if target in self._processes:
            return self._processes[target], self._process_meta.get(target)
        for pid, meta in self._process_meta.items():
            if meta.name == target:
                return self._processes.get(pid), meta
        return None, None

    # ---- Artifact inspection -----------------------------------------------

    def inspect_artifact(self, path: str, mode: str) -> ArtifactInspection:
        resolved = _resolve_safe(self._root, path)
        if not os.path.exists(resolved):
            return ArtifactInspection(
                path=path,
                mode=mode,
                success=False,
                detail=f"file not found: {path}",
            )

        metadata: dict[str, Any] = {}
        try:
            stat = os.stat(resolved)
            metadata["size_bytes"] = stat.st_size
        except OSError:
            pass

        text_modes = {"text", "json", "csv", "html", "source"}
        if mode in text_modes or not mode:
            try:
                with open(resolved, encoding="utf-8", errors="replace") as fh:
                    content = fh.read(100_000)
                metadata["backend"] = "basic"
                metadata["chars_read"] = len(content)
                return ArtifactInspection(
                    path=path,
                    mode=mode or "text",
                    success=True,
                    extracted_text=_truncate(content, _STDOUT_CAP),
                    metadata=metadata,
                    detail=f"inspected {path} ({len(content)} chars)",
                )
            except OSError as exc:
                return ArtifactInspection(
                    path=path,
                    mode=mode,
                    success=False,
                    metadata=metadata,
                    detail=f"read error: {exc}",
                )

        # Binary / unknown mode: size + first-bytes preview. This is useful
        # metadata, not semantic content. If the caller asked for an image/OCR
        # style preview, report the semantic capability gap explicitly so the
        # solver does not loop on a "successful" non-observation.
        metadata["backend"] = "basic"
        try:
            with open(resolved, "rb") as fh:
                head = fh.read(256)
            preview = head.hex(" ", 1)[:200]
            metadata["head_hex"] = preview
            metadata["semantic_content_available"] = False
            metadata["semantic_content_status"] = (
                "metadata_only: no semantic extractor is wired for this mode"
            )
            metadata_only_modes = {"auto", "preview", "image", "ocr", "pdf", "frames"}
            metadata_only = (mode or "").strip().lower() in metadata_only_modes
            return ArtifactInspection(
                path=path,
                mode=mode,
                success=not metadata_only,
                extracted_text="",
                metadata=metadata,
                detail=(
                    f"metadata-only inspection of {path} "
                    f"({metadata.get('size_bytes', '?')} bytes); semantic content unavailable"
                    if metadata_only
                    else f"binary metadata for {path} ({metadata.get('size_bytes', '?')} bytes)"
                ),
            )
        except OSError as exc:
            return ArtifactInspection(
                path=path,
                mode=mode,
                success=False,
                metadata=metadata,
                detail=f"read error: {exc}",
            )

    # ---- EnvMap refresh ----------------------------------------------------

    def refresh_envmap(self, envmap: EnvMap) -> EnvMap:
        visible_files: list[str] = []
        visible_dirs: list[str] = []
        count = 0
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not (Path(dirpath) / d).is_symlink()
            ]
            rel_dir = os.path.relpath(dirpath, self._root)
            if rel_dir != ".":
                visible_dirs.append(rel_dir)
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                visible_files.append(os.path.relpath(full, self._root))
                count += 1
                if count >= _MAX_SCAN_ENTRIES:
                    break
            if count >= _MAX_SCAN_ENTRIES:
                break

        return EnvMap(
            task_prompt=envmap.task_prompt,
            workspace_root=envmap.workspace_root,
            visible_files=tuple(sorted(visible_files)),
            visible_dirs=tuple(sorted(visible_dirs)),
            capabilities=envmap.capabilities,
            services=envmap.services,
            resource_limits=envmap.resource_limits,
            permissions=envmap.permissions,
            grader_hints=envmap.grader_hints,
            interactive_features=envmap.interactive_features,
            task_metadata=envmap.task_metadata,
            network_scope=envmap.network_scope,
        )
