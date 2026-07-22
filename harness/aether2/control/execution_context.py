"""ExecutionContext, ToolInvocationRecord, and RunResult for the Aether-2 control loop.

Pure extraction from loop.py — zero behaviour change. Public API re-exported
from loop.py so existing imports are unaffected.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from harness.aether2.hooks.registry import HookRegistry
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.traces.delta import (
    StateSnapshot,
    diff as delta_diff,
    snapshot as delta_snapshot,
)
from harness.aether2.traces.envelope import ObservationEnvelope, build_envelope
from harness.aether2.traces.envelope import FileDelta as EnvelopeFileDelta
from harness.aether2.traces.envelope import ProcessDelta
from harness.aether2.traces.mirror import MirrorNote
from harness.aether2.tools.permissions import PermissionManager
from harness.aether2.tools.registry import ToolRegistry, build_native_tool_registry
from harness.aether2.traces.receipts import _redact_text

from harness.aether2.control.action_helpers import _error_raw
from harness.aether2.control.pkg_detect import _is_package_manager_install

__all__ = [
    "ExecutionContext",
    "RunResult",
    "ToolInvocationRecord",
]

_ARTIFACT_TEXT_LIMIT = 4000
_ARTIFACT_OCR_PAGE_LIMIT = 3
_RAPID_OCR_ENGINE: Any | None = None


@dataclass(frozen=True)
class ToolInvocationRecord:
    """One dispatched tool call, its arguments, and the resulting envelope."""

    step: int
    tool_name: str
    arguments: dict[str, Any]
    envelope: ObservationEnvelope
    permission_decision: dict[str, Any] | None = None
    hook_trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RunResult:
    """The outcome of one continuity-loop run, carrying everything the scorecard needs."""

    verifier_clean: bool
    finalize_reason: str
    summary: str
    steps: int
    model_calls: int
    tokens_cached: int
    tokens_fresh: int
    cost: float
    wall_time: float
    no_delta_streaks: int
    verification_rounds: int
    recoveries: int
    compaction_count: int
    job_survival: bool
    session_survival: bool
    proof_state: dict[str, Any] | None = None
    cost_estimate: float = 0.0
    transcript_repairs: int = 0
    grader_reward: float | None = None
    reasoning_trace_ref: str | None = None
    tool_invocations: list[ToolInvocationRecord] = field(default_factory=list)
    mirror_notes: list[MirrorNote] = field(default_factory=list)
    discrepancy_reports: list[Any] = field(default_factory=list)

    @property
    def verifier_readiness(self) -> bool:
        """Advisory verifier readiness signal; official reward remains authoritative."""
        return self.verifier_clean

    @property
    def pass_(self) -> bool:
        """Deprecated alias for the advisory verifier readiness signal."""
        return self.verifier_clean


class ExecutionContext:
    """Adapts the container executor, job registry, and session registry to the dispatch surface."""

    def __init__(
        self,
        *,
        executor: ContainerExecutor,
        job_registry: JobRegistry,
        session_registry: SessionRegistry,
        raw_log_dir: Path,
        hook_registry: HookRegistry | None = None,
        permission_manager: PermissionManager | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.executor = executor
        self.job_registry = job_registry
        self.session_registry = session_registry
        self.raw_log_dir = raw_log_dir
        self.hook_registry = hook_registry or HookRegistry()
        self.permission_manager = permission_manager or PermissionManager()
        self.tool_registry = tool_registry or build_native_tool_registry()
        self.workspace_root = executor.workspace_root
        self.last_snapshot: StateSnapshot = self._capture_snapshot()
        self.last_delta_report = delta_diff(self.last_snapshot, self.last_snapshot)
        # Cumulative run-level facts for the §6.5 fact ledger (not derivable from
        # a single filesystem snapshot).
        self.installed_packages: list[str] = []
        self.nonzero_exits: list[dict[str, Any]] = []
        # In-run tool invocation records exposed to query_evidence.
        self._run_tool_invocations: list["ToolInvocationRecord"] = []
        self.receipt_store: Any | None = None
        self.task_local_tools: Any | None = None
        self.proof_state: Mapping[str, Any] | None = None
        self._warning_signatures: set[str] = set()
        self._step_primary_action: dict[str, Any] | None = None

    def run_command(self, cmd: str, timeout_sec: int = 120, cwd: str | None = None) -> ObservationEnvelope:
        raw = self.executor.run(cmd, timeout_sec=timeout_sec, cwd=cwd)
        exit_code = getattr(raw, "exit_code", None)
        if exit_code == 0 and _is_package_manager_install(cmd):
            self.installed_packages.append(cmd)
        elif exit_code is not None and exit_code != 0:
            self.nonzero_exits.append({"command": cmd, "exit_code": exit_code, "stderr": (getattr(raw, "stderr", "") or "")[-2048:]})
        return self._observe_raw(raw)

    def start_job(self, cmd: str, job_id: str | None = None, cwd: str | None = None) -> ObservationEnvelope:
        started_at = time.monotonic()
        try:
            if _looks_terminal_interactive_command(cmd):
                raw = {
                    "tool": "start_job",
                    "exit_code": 126,
                    "duration_sec": time.monotonic() - started_at,
                    "cwd": str(self.executor.workspace_root),
                    "stdout": "",
                    "stderr": (
                        "interactive_job_requires_session_start: this command appears to require an attached "
                        "terminal/stdin. Use session_start with the interactive command itself, then use "
                        "session_send/session_read."
                    ),
                    "error": {
                        "kind": "interactive_job_requires_session_start",
                        "message": (
                            "start_job is for detached non-interactive jobs. This command appears terminal-"
                            "interactive and cannot receive later keystrokes through session_send. Launch it "
                            "with session_start instead."
                        ),
                        "reason_code": "interactive_job_requires_session_start",
                        "failure_class": "tool_contract_execution",
                        "tool_name": "start_job",
                        "command": cmd,
                    },
                }
                return self._observe_raw(raw)
            if hasattr(self.executor, "start_background_job"):
                status = self.executor.start_background_job(cmd, job_id=job_id, cwd=cwd)
                resolved_job_id = status.job_id
            else:
                resolved_job_id = self.job_registry.start(cmd, job_id=job_id, cwd=cwd)
                status = self.job_registry.status(resolved_job_id)
            raw = {
                "tool": "start_job",
                "exit_code": 0,
                "duration_sec": time.monotonic() - started_at,
                "cwd": status.cwd,
                "stdout": f"started job {resolved_job_id} (pid {status.pid})",
                "stderr": "",
            }
        except Exception as exc:  # noqa: BLE001 - surfaced as a truthful error envelope
            raw = _error_raw("start_job", exc, started_at=started_at, cwd=str(self.executor.workspace_root))
        return self._observe_raw(raw)

    def job_status(self, job_id: str) -> ObservationEnvelope:
        started_at = time.monotonic()
        try:
            if hasattr(self.executor, "status_background_job"):
                status = self.executor.status_background_job(job_id)
            else:
                status = self.job_registry.status(job_id)
            raw = {
                "tool": "job_status",
                "exit_code": status.exit_code if status.exit_code is not None else (0 if status.alive else None),
                "duration_sec": time.monotonic() - started_at,
                "cwd": status.cwd,
                "stdout": (
                    f"job {job_id}: alive={status.alive} exit_code={status.exit_code}\n--- log tail ---\n{status.tail}"
                ),
                "stderr": "",
            }
        except Exception as exc:  # noqa: BLE001
            raw = _error_raw("job_status", exc, started_at=started_at, cwd=str(self.executor.workspace_root))
        return self._observe_raw(raw)

    def session_start(self, session_id: str, command: str) -> ObservationEnvelope:
        started_at = time.monotonic()
        try:
            self.session_registry.start(session_id, command)
            raw = {
                "tool": "session_start",
                "exit_code": 0,
                "duration_sec": time.monotonic() - started_at,
                "cwd": str(self.executor.workspace_root),
                "stdout": f"started session {session_id}",
                "stderr": "",
            }
        except Exception as exc:  # noqa: BLE001
            raw = _error_raw("session_start", exc, started_at=started_at, cwd=str(self.executor.workspace_root))
        return self._observe_raw(raw)

    def session_send(self, session_id: str, keys: str) -> ObservationEnvelope:
        started_at = time.monotonic()
        preservation = getattr(self, "candidate_preservation", None)
        if preservation is not None:
            block = preservation.destructive_session_send_block(session_id=session_id, keys=keys)
            if block is not None:
                raw = {
                    "tool": "session_send",
                    "exit_code": 126,
                    "duration_sec": time.monotonic() - started_at,
                    "cwd": str(self.executor.workspace_root),
                    "stdout": "",
                    "stderr": block["message"],
                    "error": block,
                }
                return self._observe_raw(raw)
        try:
            self.session_registry.send(session_id, keys)
            raw = {
                "tool": "session_send",
                "exit_code": 0,
                "duration_sec": time.monotonic() - started_at,
                "cwd": str(self.executor.workspace_root),
                "stdout": f"sent keys to session {session_id}",
                "stderr": "",
            }
        except Exception as exc:  # noqa: BLE001
            raw = _error_raw("session_send", exc, started_at=started_at, cwd=str(self.executor.workspace_root))
        return self._observe_raw(raw)

    def session_read(self, session_id: str) -> ObservationEnvelope:
        started_at = time.monotonic()
        try:
            screen = self.session_registry.read(session_id)
            raw = {
                "tool": "session_read",
                "exit_code": None,
                "duration_sec": time.monotonic() - started_at,
                "cwd": str(self.executor.workspace_root),
                "stdout": screen,
                "stderr": "",
            }
        except Exception as exc:  # noqa: BLE001
            raw = _error_raw("session_read", exc, started_at=started_at, cwd=str(self.executor.workspace_root))
        return self._observe_raw(raw)

    def read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> ObservationEnvelope:
        started_at = time.monotonic()
        try:
            if hasattr(self.executor, "read_text_file"):
                text = self.executor.read_text_file(path)
            else:
                target = self.executor.resolve_workspace_path(path)
                text = target.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            start = offset or 0
            if limit is not None:
                selected = lines[start : start + limit]
            else:
                selected = lines[start:]
            content = "".join(selected)
            truncated_note = ""
            if start > 0 or (limit is not None and start + limit < len(lines)):
                truncated_note = f"\n...[showing lines {start}:{start + len(selected)} of {len(lines)}]"
            raw = {
                "tool": "read_file",
                "exit_code": 0,
                "duration_sec": time.monotonic() - started_at,
                "cwd": str(self.executor.workspace_root),
                "stdout": content + truncated_note,
                "stderr": "",
            }
        except ValueError as exc:
            raw = _error_raw(
                "read_file",
                exc,
                started_at=started_at,
                cwd=str(self.executor.workspace_root),
                kind="workspace_boundary_violation",
            )
        except FileNotFoundError as exc:
            raw = _error_raw("read_file", exc, started_at=started_at, cwd=str(self.executor.workspace_root), kind="file_not_found")
        except OSError as exc:
            raw = _error_raw("read_file", exc, started_at=started_at, cwd=str(self.executor.workspace_root), kind="io_error")
        return self._observe_raw(raw)

    def inspect_artifact(
        self,
        path: str,
        question: str | None = None,
        mode: str = "auto",
        max_outputs: int = 5,
    ) -> ObservationEnvelope:
        started_at = time.monotonic()
        bounded_outputs = max(1, min(int(max_outputs), 20))
        mode = (mode or "auto").strip().lower()
        if mode not in {"auto", "text", "ocr", "vision", "pdf", "frames", "metadata"}:
            mode = "auto"
        try:
            target = self.executor.resolve_workspace_path(path)
            if not target.exists():
                raise FileNotFoundError(path)
            stat = target.stat()
            suffix = target.suffix.lower()
            kind = _artifact_kind(suffix)
            summary: dict[str, Any] = {
                "path": path,
                "kind": kind,
                "mode": mode,
                "size_bytes": stat.st_size,
                "question": question or "",
                "outputs": [],
            }
            if mode in {"auto", "metadata"}:
                summary["outputs"].append({"type": "metadata", "suffix": suffix, "size_bytes": stat.st_size})
            if kind == "text" or mode == "text":
                text = target.read_text(encoding="utf-8", errors="replace")
                summary["outputs"].append({"type": "text_excerpt", "text": text[:4000]})
            elif kind == "pdf":
                pdf_text, pdf_source, pdf_note = _inspect_pdf_content(target, max_chars=_ARTIFACT_TEXT_LIMIT)
                if pdf_text:
                    summary["outputs"].append(
                        {
                            "type": "text_excerpt",
                            "source": pdf_source,
                            "status": "content_available",
                            "text": pdf_text,
                            "ocr_required_or_unavailable": False,
                        }
                    )
                else:
                    summary["outputs"].append(
                        {
                            "type": "pdf",
                            "status": "metadata_only",
                            "ocr_required_or_unavailable": True,
                            "note": pdf_note or "PDF text extraction unavailable in this generic path",
                        }
                    )
            elif kind == "image":
                ocr_text, ocr_note = _inspect_image_content(target, max_chars=_ARTIFACT_TEXT_LIMIT)
                if ocr_text:
                    summary["outputs"].append(
                        {
                            "type": "text_excerpt",
                            "source": "ocr",
                            "status": "content_available",
                            "text": ocr_text,
                            "ocr_text_extracted": True,
                        }
                    )
                else:
                    summary["outputs"].append(
                        {
                            "type": "image",
                            "status": "metadata_only",
                            "ocr_backend_unavailable": True,
                            "note": ocr_note or "OCR/vision unavailable unless environment tools support it",
                        }
                    )
            elif kind == "video":
                summary["outputs"].append(_inspect_video_content(target))
            else:
                summary["outputs"].append({"type": "binary", "status": "unsupported_binary"})
            summary["outputs"] = summary["outputs"][:bounded_outputs]
            receipt_store = getattr(self, "receipt_store", None)
            if receipt_store is not None:
                receipt_store.record_artifact_observation(
                    step=None,
                    path=path,
                    mode=mode,
                    status="ok",
                    summary=f"inspect_artifact {path}: {kind}",
                    payload=summary,
                )
            raw = {
                "tool": "inspect_artifact",
                "exit_code": 0,
                "duration_sec": time.monotonic() - started_at,
                "cwd": str(self.executor.workspace_root),
                "stdout": json.dumps(summary, sort_keys=True, ensure_ascii=True),
                "stderr": "",
            }
        except ValueError as exc:
            raw = _error_raw(
                "inspect_artifact",
                exc,
                started_at=started_at,
                cwd=str(self.executor.workspace_root),
                kind="workspace_boundary_violation",
            )
        except FileNotFoundError as exc:
            raw = _error_raw("inspect_artifact", exc, started_at=started_at, cwd=str(self.executor.workspace_root), kind="file_not_found")
        except OSError as exc:
            raw = _error_raw("inspect_artifact", exc, started_at=started_at, cwd=str(self.executor.workspace_root), kind="io_error")
        return self._observe_raw(raw)

    def write_file(self, path: str, content: str) -> ObservationEnvelope:
        started_at = time.monotonic()
        try:
            if hasattr(self.executor, "write_text_file"):
                self.executor.write_text_file(path, content)
            else:
                target = self.executor.resolve_workspace_path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = target.with_name(target.name + ".aether2_tmp")
                tmp_path.write_text(content, encoding="utf-8")
                tmp_path.replace(target)
            raw = {
                "tool": "write_file",
                "exit_code": 0,
                "duration_sec": time.monotonic() - started_at,
                "cwd": str(self.executor.workspace_root),
                "stdout": f"wrote {len(content)} bytes to {path}",
                "stderr": "",
            }
        except ValueError as exc:
            raw = _error_raw(
                "write_file",
                exc,
                started_at=started_at,
                cwd=str(self.executor.workspace_root),
                kind="workspace_boundary_violation",
            )
        except OSError as exc:
            raw = _error_raw("write_file", exc, started_at=started_at, cwd=str(self.executor.workspace_root), kind="io_error")
        return self._observe_raw(raw)

    def wait(self, seconds: int, reason: str) -> ObservationEnvelope:
        started_at = time.monotonic()
        bounded_seconds = max(0, min(int(seconds), 300))
        time.sleep(bounded_seconds)
        raw = {
            "tool": "wait",
            "exit_code": 0,
            "duration_sec": time.monotonic() - started_at,
            "cwd": str(self.executor.workspace_root),
            "stdout": f"waited {bounded_seconds}s ({reason})",
            "stderr": "",
        }
        return self._observe_raw(raw)

    def task_done(
        self,
        summary: str,
        checks: list[str],
        requirements: list[dict[str, Any]] | None = None,
        limitations: list[str] | None = None,
    ) -> ObservationEnvelope:
        del checks, requirements, limitations
        raw = {
            "tool": "task_done",
            "exit_code": 0,
            "duration_sec": 0.0,
            "cwd": str(self.executor.workspace_root),
            "stdout": summary,
            "stderr": "",
        }
        return self._observe_raw(raw)

    def task_blocked(
        self,
        blocker: str,
        evidence: list[str],
        attempts: list[str],
        missing_external_state: list[str],
        recommended_next_evidence: list[str],
        limitations: list[str] | None = None,
    ) -> ObservationEnvelope:
        del evidence, attempts, missing_external_state, recommended_next_evidence, limitations
        raw = {
            "tool": "task_blocked",
            "exit_code": 0,
            "duration_sec": 0.0,
            "cwd": str(self.executor.workspace_root),
            "stdout": blocker,
            "stderr": "",
        }
        return self._observe_raw(raw)

    def observe_synthetic(self, raw: Mapping[str, Any]) -> ObservationEnvelope:
        return self._observe_raw(raw)

    def query_evidence(self, query: str, tool: str | None = None, limit: int = 10) -> ObservationEnvelope:
        """Search current-run evidence from prior tool invocations by keyword."""
        import time as _time
        started_at = _time.monotonic()

        _SNIPPET_LEN = 300
        _ARGS_SUMMARY_LEN = 120
        _TOTAL_OUTPUT_BUDGET = 8000

        query_lower = query.lower().strip()
        limit = max(1, min(int(limit), 50))

        matches: list[dict[str, Any]] = []
        receipt_store = getattr(self, "receipt_store", None)
        if receipt_store is not None:
            for event in receipt_store.query(query, event_type=None, limit=limit):
                payload = event.get("payload", {}) if isinstance(event, Mapping) else {}
                if tool is not None and payload.get("tool_name") != tool and event.get("event_type") != tool:
                    continue
                matches.append(
                    {
                        "source": "receipt_store",
                        "event_id": event.get("event_id"),
                        "event_type": event.get("event_type"),
                        "step": event.get("step"),
                        "summary": _redact_text(str(event.get("summary", "")))[:_SNIPPET_LEN],
                        "payload_keys": sorted(payload.keys()) if isinstance(payload, Mapping) else [],
                    }
                )
                if len(matches) >= limit:
                    break

        local_tools = getattr(self, "task_local_tools", None)
        if local_tools is not None and len(matches) < limit:
            summary = local_tools.summary(limit=20)
            blob = json.dumps(summary, sort_keys=True, ensure_ascii=True).lower()
            if query_lower in blob and (tool is None or tool == "task_local_tools"):
                matches.append(
                    {
                        "source": "task_local_tools",
                        "event_type": "task_local_tools",
                        "step": None,
                        "summary": _redact_text(json.dumps(summary, sort_keys=True, ensure_ascii=True))[:_SNIPPET_LEN],
                    }
                )

        for record in reversed(self._run_tool_invocations):
            if len(matches) >= limit:
                break
            # Skip self-search invocations to prevent noise loops.
            if record.tool_name in {"query_history", "query_evidence"}:
                continue
            # Apply optional tool-name filter.
            if tool is not None and record.tool_name != tool:
                continue

            # Build a searchable text blob from the record's tool name, args, and output.
            args_text = json.dumps(record.arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            stdout_text = (record.envelope.stdout_head or "") + (record.envelope.stdout_tail or "")
            stderr_text = (record.envelope.stderr_head or "") + (record.envelope.stderr_tail or "")
            haystack = " ".join([record.tool_name, args_text, stdout_text, stderr_text]).lower()

            if query_lower and query_lower not in haystack:
                continue

            # Build a compact, redacted entry.
            args_summary = _redact_text(args_text)
            if len(args_summary) > _ARGS_SUMMARY_LEN:
                args_summary = args_summary[:_ARGS_SUMMARY_LEN] + "..."
            output_snippet = _redact_text((stdout_text + stderr_text).strip())
            if len(output_snippet) > _SNIPPET_LEN:
                output_snippet = output_snippet[:_SNIPPET_LEN] + "..."
            matches.append(
                {
                    "step": record.step,
                    "tool": record.tool_name,
                    "args_summary": args_summary,
                    "output_snippet": output_snippet,
                    "exit_code": record.envelope.exit_code,
                }
            )
            if len(matches) >= limit:
                break

        if not matches:
            result_text = f"no matching current-run evidence for query={query!r}"
            if tool is not None:
                result_text += f" tool={tool!r}"
        else:
            parts = [f"query_evidence: {len(matches)} result(s) for {query!r}"]
            total = len(parts[0])
            for entry in matches:
                line = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                if total + len(line) > _TOTAL_OUTPUT_BUDGET:
                    parts.append("...[output budget reached]")
                    break
                parts.append(line)
                total += len(line)
            result_text = "\n".join(parts)

        raw = {
            "tool": "query_evidence",
            "exit_code": 0,
            "duration_sec": _time.monotonic() - started_at,
            "cwd": str(self.executor.workspace_root),
            "stdout": result_text,
            "stderr": "",
        }
        return self._observe_raw(raw)

    def query_history(self, query: str, tool: str | None = None, limit: int = 10) -> ObservationEnvelope:
        """Backward-compatible alias for query_evidence."""
        return self.query_evidence(query=query, tool=tool, limit=limit)

    def _capture_snapshot(self) -> StateSnapshot:
        refresh = getattr(self.executor, "prepare_snapshot", None)
        if callable(refresh):
            refresh()
        return delta_snapshot(self.workspace_root)

    def _observe_raw(self, raw: Any) -> ObservationEnvelope:
        current_snapshot = self._capture_snapshot()
        delta_report = delta_diff(self.last_snapshot, current_snapshot)
        raw_payload = dict(raw) if isinstance(raw, Mapping) else raw.__dict__.copy()
        raw_payload["files_changed"] = [
            EnvelopeFileDelta(
                path=item.path,
                hash_before=item.hash_before,
                hash_after=item.hash_after,
                change_type=item.change_type,
            )
            for item in delta_report.files_changed
        ]
        raw_payload["process_delta"] = self._build_process_delta(self.last_snapshot, current_snapshot)
        envelope = build_envelope(raw_payload, raw_log_dir=self.raw_log_dir)
        self.last_snapshot = current_snapshot
        self.last_delta_report = delta_report
        return envelope

    def _build_process_delta(self, prev: StateSnapshot, curr: StateSnapshot) -> ProcessDelta:
        process_delta = ProcessDelta()

        prev_jobs = prev.job_registry
        curr_jobs = curr.job_registry
        prev_sessions = prev.session_registry
        curr_sessions = curr.session_registry
        prev_services = prev.service_registry
        curr_services = curr.service_registry
        prev_processes = prev.process_registry
        curr_processes = curr.process_registry

        for job_id in sorted(set(prev_jobs) | set(curr_jobs)):
            before = prev_jobs.get(job_id)
            after = curr_jobs.get(job_id)
            if before is None and after is not None:
                process_delta.jobs_started.append(job_id)
            elif before is not None and after is None:
                process_delta.jobs_exited.append(job_id)
            elif before is not None and after is not None:
                if bool(before.get("alive")) and not bool(after.get("alive")):
                    process_delta.jobs_exited.append(job_id)
                growth = int(after.get("log_size", 0)) - int(before.get("log_size", 0))
                if growth > 0:
                    process_delta.job_log_growth[job_id] = growth

        for session_id in sorted(set(prev_sessions) | set(curr_sessions)):
            before = prev_sessions.get(session_id)
            after = curr_sessions.get(session_id)
            if before is None and after is not None:
                process_delta.sessions_started.append(session_id)
            elif before is not None and after is None:
                process_delta.sessions_exited.append(session_id)

        for service_id in sorted(set(prev_services) | set(curr_services)):
            before = prev_services.get(service_id)
            after = curr_services.get(service_id)
            if before is None and after is not None:
                process_delta.services_started.append(service_id)
            elif before is not None and after is None:
                process_delta.services_exited.append(service_id)

        for process_id in sorted(set(prev_processes) | set(curr_processes)):
            before = prev_processes.get(process_id)
            after = curr_processes.get(process_id)
            if before is None and after is not None:
                process_delta.started.append(process_id)
            elif before is not None and after is None:
                process_delta.exited.append(process_id)

        return process_delta


def _looks_terminal_interactive_command(cmd: str) -> bool:
    lowered = f" {cmd.lower()} "
    return any(
        marker in lowered
        for marker in (
            " -nographic",
            " -serial mon:stdio",
            " -serial stdio",
            " mon:stdio",
        )
    )


def _artifact_kind(suffix: str) -> str:
    if suffix in {".txt", ".md", ".json", ".jsonl", ".csv", ".tsv", ".py", ".js", ".html", ".xml", ".yaml", ".yml"}:
        return "text"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        return "image"
    if suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        return "video"
    return "binary"


def _clip_artifact_text(text: str, *, max_chars: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "..."


def _inspect_pdf_content(target: Path, *, max_chars: int) -> tuple[str | None, str | None, str | None]:
    direct_text, direct_note = _extract_pdf_text(target, max_chars=max_chars)
    if direct_text:
        return direct_text, "pdf_text", None
    ocr_text, ocr_note = _ocr_pdf_pages(target, max_chars=max_chars)
    if ocr_text:
        return ocr_text, "pdf_ocr", None
    note_parts = [part for part in (direct_note, ocr_note) if part]
    return None, None, "; ".join(note_parts) if note_parts else None


def _extract_pdf_text(target: Path, *, max_chars: int) -> tuple[str | None, str | None]:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return None, "PyMuPDF backend unavailable"
    try:
        doc = fitz.open(target)
        try:
            parts: list[str] = []
            total = 0
            for page in doc:
                text = str(page.get_text("text") or "").strip()
                if not text:
                    continue
                parts.append(text)
                total += len(text)
                if total >= max_chars:
                    break
            combined = _clip_artifact_text("\n\n".join(parts), max_chars=max_chars)
            if combined:
                return combined, None
            return None, "PDF contains no extractable text"
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001
        return None, f"PDF text extraction failed: {exc}"


def _inspect_image_content(target: Path, *, max_chars: int) -> tuple[str | None, str | None]:
    return _ocr_image_text(target, max_chars=max_chars)


def _ocr_pdf_pages(target: Path, *, max_chars: int) -> tuple[str | None, str | None]:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return None, "PyMuPDF backend unavailable for PDF OCR"
    try:
        doc = fitz.open(target)
        try:
            parts: list[str] = []
            with tempfile.TemporaryDirectory(prefix="aether2_pdf_ocr_") as tmp_dir:
                tmp_root = Path(tmp_dir)
                for page_index, page in enumerate(doc):
                    if page_index >= _ARTIFACT_OCR_PAGE_LIMIT:
                        break
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    image_path = tmp_root / f"page_{page_index + 1}.png"
                    pixmap.save(str(image_path))
                    text, _ = _ocr_image_text(image_path, max_chars=max_chars)
                    if text:
                        parts.append(text)
                    if sum(len(item) for item in parts) >= max_chars:
                        break
            combined = _clip_artifact_text("\n\n".join(parts), max_chars=max_chars)
            if combined:
                return combined, None
            return None, "PDF OCR unavailable or produced no text"
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001
        return None, f"PDF OCR failed: {exc}"


def _rapidocr_engine() -> Any:
    global _RAPID_OCR_ENGINE
    if _RAPID_OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

        _RAPID_OCR_ENGINE = RapidOCR()
    return _RAPID_OCR_ENGINE


def _ocr_image_text(target: Path, *, max_chars: int) -> tuple[str | None, str | None]:
    try:
        engine = _rapidocr_engine()
    except Exception as exc:  # noqa: BLE001
        return None, f"OCR backend unavailable: {exc}"
    try:
        result, _elapsed = engine(str(target))
    except Exception as exc:  # noqa: BLE001
        return None, f"OCR failed: {exc}"
    lines: list[str] = []
    for row in result or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        text = str(row[1] or "").strip()
        if text:
            lines.append(text)
    combined = _clip_artifact_text("\n".join(lines), max_chars=max_chars)
    if combined:
        return combined, None
    return None, "OCR unavailable unless environment tools support it"


def _inspect_video_content(target: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "video",
        "status": "metadata_only",
        "semantic_video_analysis_missing": True,
        "transcript_unavailable": True,
        "sample_frames_extracted": False,
    }
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,r_frame_rate,nb_frames",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        payload["note"] = f"video metadata backend unavailable: {exc}"
        return payload
    if completed.returncode != 0:
        payload["note"] = f"video metadata extraction failed: {(completed.stderr or completed.stdout).strip()}"
        return payload
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        payload["note"] = f"video metadata parse failed: {exc}"
        return payload
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    stream = streams[0] if streams else {}
    fmt = data.get("format") if isinstance(data.get("format"), Mapping) else {}
    width = stream.get("width")
    height = stream.get("height")
    fps_raw = str(stream.get("r_frame_rate", "") or "")
    duration = fmt.get("duration")
    frame_count = stream.get("nb_frames")
    if isinstance(width, int) and isinstance(height, int):
        payload["resolution"] = f"{width}x{height}"
    payload["fps"] = _parse_fractional_rate(fps_raw)
    payload["duration_seconds"] = _safe_float(duration)
    payload["frame_count"] = _safe_int(frame_count)
    payload["note"] = "sample frame extraction unavailable unless generic video tooling is installed"
    return payload


def _parse_fractional_rate(value: str) -> float | None:
    compact = str(value or "").strip()
    if not compact:
        return None
    if "/" in compact:
        numerator, denominator = compact.split("/", 1)
        try:
            denom = float(denominator)
            if denom == 0:
                return None
            return round(float(numerator) / denom, 3)
        except ValueError:
            return None
    try:
        return round(float(compact), 3)
    except ValueError:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
