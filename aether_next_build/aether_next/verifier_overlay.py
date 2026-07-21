"""Sandboxed verifier overlay: execute checks and fixtures against a copy.

The verifier may execute declared checks, its own fixture files, and bounded
commands against a copy of the solver workspace.  The copy is created and
destroyed through the SAME executor substrate the solver used (host bash or
``docker exec``), so overlay runs see the identical toolchain and filesystem
semantics.  The solver workspace is never mutated by verification, and the
overlay is always rolled back (deleted) when the verification round ends.

This is a generic capability class -- no task-specific logic belongs here.
"""
from __future__ import annotations

import base64
import posixpath
import uuid
from typing import Any


_OVERLAY_COMMAND_TIMEOUT_S = 300


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


class VerifierOverlay:
    """Copy-on-demand sandbox for verifier-owned execution.

    Lifecycle: lazily created on first use, torn down unconditionally by the
    caller (``kernel_verifier`` wraps verification in try/finally).  All
    filesystem effects of overlay commands land in the overlay directory,
    which is a sibling of the workspace root and therefore outside the
    solver-visible tree and outside executor mtime snapshots.
    """

    def __init__(
        self,
        executor: Any,
        workspace_root: str,
        *,
        max_command_timeout_s: int = _OVERLAY_COMMAND_TIMEOUT_S,
    ) -> None:
        self._executor = executor
        self._workspace_root = workspace_root.rstrip("/") or "/"
        self._overlay_root: str | None = None
        self._overlay_executor: Any | None = None
        self._setup_error: str = ""
        self._max_command_timeout_s = max(30, int(max_command_timeout_s))

    @property
    def overlay_root(self) -> str:
        return self._overlay_root or ""

    def ensure(self) -> dict[str, Any]:
        """Create the overlay copy if needed; report truthfully on failure."""
        if self._overlay_root is not None:
            return {"overlay_root": self._overlay_root, "created": False}
        if self._setup_error:
            return {"error": self._setup_error}
        candidate = f"{self._workspace_root}.verifier_overlay_{uuid.uuid4().hex[:8]}"
        copy_cmd = (
            f"rm -rf {_shell_quote(candidate)} && "
            f"cp -a {_shell_quote(self._workspace_root)} {_shell_quote(candidate)}"
        )
        result = self._executor.run_command(copy_cmd, timeout_s=_OVERLAY_COMMAND_TIMEOUT_S)
        if not result.success:
            self._setup_error = (
                f"overlay setup failed (exit={result.exit_code}): "
                f"{(result.stderr or result.stdout)[:500]}"
            )
            return {"error": self._setup_error}
        factory = getattr(self._executor, "for_workspace", None)
        if not callable(factory):
            self._setup_error = "executor cannot create a constrained overlay workspace"
            self._executor.run_command(f"rm -rf {_shell_quote(candidate)}", timeout_s=120)
            return {"error": self._setup_error}
        self._overlay_root = candidate
        self._overlay_executor = factory(candidate)
        return {"overlay_root": candidate, "created": True}

    def run_command(self, command: str, *, timeout_s: int | None = None) -> dict[str, Any]:
        """Run a command with the overlay as working directory.

        The ceiling is the task-declared verifier budget when the caller
        provided one at construction; a fixed default otherwise."""
        state = self.ensure()
        if "error" in state:
            return {"error": state["error"]}
        requested = self._max_command_timeout_s if timeout_s is None else int(timeout_s)
        assert self._overlay_executor is not None
        result = self._overlay_executor.run_command(
            command,
            timeout_s=max(1, min(requested, self._max_command_timeout_s)),
        )
        return {
            "overlay_root": self._overlay_root,
            "command": command,
            "exit_code": result.exit_code,
            "success": result.success,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:4000],
            "timed_out": getattr(result, "timed_out", False),
        }

    def write_fixture(self, relpath: str, content: str) -> dict[str, Any]:
        """Write a verifier-authored fixture INTO THE OVERLAY only."""
        state = self.ensure()
        if "error" in state:
            return {"error": state["error"]}
        clean = posixpath.normpath(relpath.lstrip("/"))
        if clean.startswith("app/"):
            clean = clean[4:]
        elif clean == "app":
            clean = "."
        if clean.startswith("..") or clean in {".", ""}:
            return {"error": f"invalid fixture path: {relpath!r}"}
        target = posixpath.join(self._overlay_root, clean)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        parent = posixpath.dirname(target) or self._overlay_root
        cmd = (
            f"mkdir -p {_shell_quote(parent)} && "
            f"printf '%s' {_shell_quote(encoded)} | base64 -d > {_shell_quote(target)}"
        )
        assert self._overlay_executor is not None
        result = self._overlay_executor.run_command(cmd, timeout_s=60)
        if not result.success:
            return {"error": f"fixture write failed: {(result.stderr or result.stdout)[:500]}"}
        return {
            "overlay_root": self._overlay_root,
            "fixture_path": clean,
            "bytes": len(content.encode("utf-8")),
            "written": True,
        }

    def teardown(self) -> dict[str, Any]:
        """Delete the overlay; rollback is unconditional and idempotent."""
        if self._overlay_root is None:
            return {"removed": False}
        target, self._overlay_root = self._overlay_root, None
        self._overlay_executor = None
        result = self._executor.run_command(
            f"rm -rf {_shell_quote(target)}", timeout_s=120,
        )
        return {"removed": result.success, "overlay_root": target}
