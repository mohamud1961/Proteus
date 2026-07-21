"""Tmux-backed session registry for persistent PTY interaction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shutil
import subprocess

from harness.aether2.runtime.executor import ContainerBackend


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    command: str
    registry_path: str


class SessionRegistry:
    """Tmux-backed session registry (C1).

    When `backend.kind == "local"` (the default), `tmux` is invoked on the
    host exactly as before. When `backend.kind == "docker"`, every `tmux`
    invocation is routed through `docker exec <container> tmux ...` so the
    PTY session lives inside the task container, where the agent and verifier
    run. If `tmux` is unavailable in-container, callers get a truthful error
    (the same `RuntimeError("tmux is unavailable")` as the local case).
    """

    def __init__(self, state_dir: Path, *, backend: ContainerBackend | None = None) -> None:
        self.state_dir = state_dir.resolve()
        self.sessions_dir = self.state_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.backend = backend or ContainerBackend()

    def start(self, session_id: str, command: str) -> str:
        self._require_tmux()
        path = self.sessions_dir / f"{session_id}.json"
        if path.exists():
            raise ValueError(f"session already exists: {session_id}")
        self._tmux("new-session", "-d", "-s", session_id, command)
        record = SessionRecord(
            session_id=session_id,
            command=command,
            registry_path=str(path),
        )
        path.write_text(
            json.dumps(record.__dict__, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return session_id

    def send(self, session_id: str, keys: str) -> None:
        self._require_tmux()
        self._load(session_id)
        self._tmux("send-keys", "-t", session_id, keys)

    def read(self, session_id: str) -> str:
        self._require_tmux()
        self._load(session_id)
        try:
            result = self._tmux("capture-pane", "-p", "-t", session_id)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            if "can't find" in stderr.lower() or "no such" in stderr.lower():
                return ""
            raise
        return result.stdout

    def stop(self, session_id: str) -> None:
        """Kill the underlying tmux session and remove it from the registry, if present."""
        self._require_tmux()
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            raise KeyError(f"unknown session: {session_id}")
        try:
            self._tmux("kill-session", "-t", session_id)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            if "can't find" not in stderr.lower() and "no such" not in stderr.lower():
                raise
        path.unlink()

    def list_session_ids(self) -> list[str]:
        return sorted(path.stem for path in self.sessions_dir.glob("*.json"))

    def _load(self, session_id: str) -> SessionRecord:
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            raise KeyError(f"unknown session: {session_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionRecord(
            session_id=str(data["session_id"]),
            command=str(data["command"]),
            registry_path=str(path),
        )

    def _require_tmux(self) -> str:
        if self.backend.kind != "local":
            # Existence is checked by actually invoking tmux in-container
            # (see _tmux); we cannot probe PATH from the host.
            return "tmux"
        tmux = shutil.which("tmux")
        if not tmux:
            raise RuntimeError("tmux is unavailable")
        return tmux

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        tmux = self._require_tmux()
        if self.backend.kind == "local":
            command = [tmux, *args]
        else:
            if not self.backend.container_id:
                raise RuntimeError("docker backend requires a container_id")
            command = ["docker", "exec", self.backend.container_id, "tmux", *args]
        try:
            return subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "")
            if self.backend.kind != "local" and (
                "executable file not found" in stderr.lower() or "tmux: not found" in stderr.lower() or "no such file or directory" in stderr.lower()
            ):
                raise RuntimeError("tmux is unavailable") from exc
            raise
