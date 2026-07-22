"""Read-only verification context for the Aether-2 control loop (C7).

Deny-by-default: run_command is rejected unless every pipe segment's first
token is a conservative read-only binary AND no shell metacharacters other
than `|` (pipe) appear anywhere in the command. Every attempt -- allowed or
rejected -- is recorded via receipts.record_verifier_command for a full
audit trail. Perfect read-only enforcement in a shell is not possible (e.g.
a malicious `find -exec`); this is a best-effort structural guard plus a
complete audit trail, per the spec's honest-engineering posture.
"""

from __future__ import annotations

import shlex
from typing import Any

from harness.aether2.traces.envelope import ObservationEnvelope, build_envelope

__all__ = ["_ReadOnlyVerificationContext"]


class _ReadOnlyVerificationContext:
    """Expose only read-only inspection affordances during verifier probes (C7).

    Deny-by-default: `run_command` is rejected unless every pipe segment's first
    token is a conservative read-only binary AND no shell metacharacters other
    than `|` (pipe) appear anywhere in the command. Every attempt -- allowed or
    rejected -- is recorded via `receipts.record_verifier_command` for a full
    audit trail. Perfect read-only enforcement in a shell is not possible (e.g.
    a malicious `find -exec`); this is a best-effort structural guard plus a
    complete audit trail, per the spec's honest-engineering posture.
    """

    _ALLOWED_BINARIES = {
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "stat",
        "wc",
        "file",
        "ps",
        "df",
        "du",
        "sha256sum",
        "jq",
        "pwd",
    }
    # Any of these appearing as standalone tokens (other than "|") makes the
    # command rejected: redirects, command chaining/substitution, backgrounding.
    _DISALLOWED_TOKENS = {
        ">", ">>", "<", "<<", ";", "&", "&&", "||", "`", "$(",
        "rm", "mv", "cp", "tee", "chmod", "chown", "mkdir", "touch", "kill",
        "dd", "truncate", "sed", "-exec", "-delete", "-ok",
    }

    def __init__(self, ctx: Any, receipts: Any = None) -> None:
        self._ctx = ctx
        self._receipts = receipts
        self._call_idx = 0

    def _record(self, tool_name: str, arguments: dict[str, Any], envelope: ObservationEnvelope) -> ObservationEnvelope:
        self._call_idx += 1
        if self._receipts is not None:
            try:
                self._receipts.record_verifier_command(self._call_idx, tool_name, arguments, envelope)
            except Exception:  # noqa: BLE001 - receipts must never break verification
                pass
        return envelope

    def _rejected(self, cmd: str, message: str) -> ObservationEnvelope:
        return build_envelope(
            {
                "tool": "run_command",
                "exit_code": 1,
                "duration_sec": 0.0,
                "cwd": str(self._ctx.executor.workspace_root),
                "stdout": "",
                "stderr": message,
                "error": {
                    "kind": "verification_read_only_violation",
                    "message": message,
                    "reason_code": "verification_read_only_violation",
                    "command": cmd,
                },
            },
            raw_log_dir=self._ctx.raw_log_dir,
        )

    def run_command(self, cmd: str, timeout_sec: int = 120, cwd: str | None = None) -> ObservationEnvelope:
        """Run a read-only command inside the verification context."""

        try:
            tokens = shlex.split(cmd, posix=True)
        except ValueError:
            envelope = self._rejected(cmd, "verifier inspection command could not be parsed")
            return self._record("run_command", {"cmd": cmd, "timeout_sec": timeout_sec, "cwd": cwd}, envelope)

        if not tokens or any(token in self._DISALLOWED_TOKENS for token in tokens):
            envelope = self._rejected(cmd, "verifier inspection is read-only; command rejected")
            return self._record("run_command", {"cmd": cmd, "timeout_sec": timeout_sec, "cwd": cwd}, envelope)

        # Split on pipe tokens into segments; each segment's leading token must
        # be an allowed read-only binary.
        segments: list[list[str]] = [[]]
        for token in tokens:
            if token == "|":
                segments.append([])
            else:
                segments[-1].append(token)
        if any(not segment or segment[0] not in self._ALLOWED_BINARIES for segment in segments):
            envelope = self._rejected(cmd, "verifier inspection is read-only; command rejected")
            return self._record("run_command", {"cmd": cmd, "timeout_sec": timeout_sec, "cwd": cwd}, envelope)

        envelope = self._ctx.run_command(cmd, timeout_sec=timeout_sec, cwd=cwd)
        return self._record("run_command", {"cmd": cmd, "timeout_sec": timeout_sec, "cwd": cwd}, envelope)

    def read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> ObservationEnvelope:
        """Read a file, recording the call in the verifier audit trail."""

        envelope = self._ctx.read_file(path, offset=offset, limit=limit)
        return self._record("read_file", {"path": path, "offset": offset, "limit": limit}, envelope)

    def job_status(self, job_id: str) -> ObservationEnvelope:
        """Return job status, recording the call in the verifier audit trail."""

        envelope = self._ctx.job_status(job_id)
        return self._record("job_status", {"job_id": job_id}, envelope)

    def session_read(self, session_id: str) -> ObservationEnvelope:
        """Read session output, recording the call in the verifier audit trail."""

        envelope = self._ctx.session_read(session_id)
        return self._record("session_read", {"session_id": session_id}, envelope)
