"""Thin candidate-preservation layer for receipt-driven service/runtime runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
import json
import re


_PORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost)[: ](?P<port>\d{2,5})")
_VIABLE_MARKERS = (
    "LISTEN_OK",
    "Connected to 127.0.0.1",
    "Connected to localhost",
    "connection established",
)
_CONNECTED_LINE_RE = re.compile(r"(?im)^\s*connected\s*$")
_DESTRUCTIVE_KEYS = ("\x03", "\u0003")


@dataclass
class Candidate:
    candidate_id: str
    session_id: str | None = None
    job_id: str | None = None
    command: str = ""
    ports: set[str] = field(default_factory=set)
    viability_evidence: list[str] = field(default_factory=list)
    locked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "command": self.command,
            "ports": sorted(self.ports),
            "viability_evidence": list(self.viability_evidence),
            "locked": self.locked,
        }


class CandidatePreservation:
    """Records and protects potentially valuable live runtime candidates."""

    def __init__(self, *, receipt_store: Any | None = None) -> None:
        self.receipt_store = receipt_store
        self._candidates: dict[str, Candidate] = {}
        self._last_session_id: str | None = None
        self._last_candidate_id: str | None = None

    def active_candidates(self) -> list[dict[str, Any]]:
        return [candidate.as_dict() for candidate in self._candidates.values() if candidate.locked]

    def observe_invocation(self, record: Any) -> None:
        tool_name = str(getattr(record, "tool_name", ""))
        arguments = dict(getattr(record, "arguments", {}) or {})
        envelope = getattr(record, "envelope", None)
        stdout = ((getattr(envelope, "stdout_head", "") or "") + (getattr(envelope, "stdout_tail", "") or ""))
        stderr = ((getattr(envelope, "stderr_head", "") or "") + (getattr(envelope, "stderr_tail", "") or ""))
        exit_code = getattr(envelope, "exit_code", None)
        step = getattr(record, "step", None)

        if tool_name == "session_start" and exit_code in {0, None}:
            session_id = str(arguments.get("session_id") or "")
            command = str(arguments.get("command") or "")
            if session_id and not _looks_like_probe_client(command):
                self._last_session_id = session_id
                candidate = self._candidates.setdefault(
                    session_id,
                    Candidate(candidate_id=session_id, session_id=session_id, command=command),
                )
                candidate.command = command or candidate.command
                candidate.ports.update(_ports_from_text(command))
                self._last_candidate_id = session_id
                self._record_event(step, "candidate_started", candidate, "session candidate started")
            return

        if tool_name == "start_job" and exit_code in {0, None}:
            job_id = str(arguments.get("job_id") or "")
            command = str(arguments.get("cmd") or "")
            if job_id:
                candidate = self._candidates.setdefault(
                    job_id,
                    Candidate(candidate_id=job_id, job_id=job_id, command=command),
                )
                candidate.job_id = job_id
                candidate.command = command or candidate.command
                candidate.ports.update(_ports_from_text(command))
                self._last_candidate_id = job_id
                self._record_event(step, "candidate_started", candidate, "job candidate started")
            return

        if tool_name in {"run_command", "session_read", "job_status"}:
            blob = "\n".join([str(arguments), stdout, stderr])
            if not _looks_viable(blob):
                return
            candidate = self._candidate_for_viability(blob)
            if candidate is None:
                return
            candidate.ports.update(_ports_from_text(blob))
            evidence = _evidence_summary(tool_name, blob)
            if evidence not in candidate.viability_evidence:
                candidate.viability_evidence.append(evidence)
            candidate.locked = True
            self._record_event(step, "candidate_viable_locked", candidate, evidence)

    def destructive_session_send_block(self, *, session_id: str, keys: str) -> dict[str, Any] | None:
        candidate = self._candidates.get(session_id)
        if candidate is None or not candidate.locked:
            return None
        if not any(marker in keys for marker in _DESTRUCTIVE_KEYS):
            return None
        self._record_event(None, "candidate_destructive_input_blocked", candidate, "blocked destructive input to protected candidate")
        guidance = (
            "A destructive action was blocked because it targeted a protected viable candidate. "
            "Use non-destructive probes or create a replacement before invalidating it."
        )
        return {
            "kind": "candidate_preservation_block",
            "message": (
                f"Refusing destructive input to protected candidate session {session_id!r}. "
                "A live candidate has viability evidence; preserve it or start a replacement before interrupting it. "
                + guidance
            ),
            "recovery_guidance": guidance,
            "candidate": candidate.as_dict(),
        }

    def _candidate_for_viability(self, blob: str) -> Candidate | None:
        ports = _ports_from_text(blob)
        for candidate in reversed(list(self._candidates.values())):
            if ports and candidate.ports and ports.intersection(candidate.ports):
                return candidate
        if self._last_session_id and self._last_session_id in self._candidates:
            return self._candidates[self._last_session_id]
        if self._last_candidate_id and self._last_candidate_id in self._candidates:
            return self._candidates[self._last_candidate_id]
        return None

    def _record_event(self, step: int | None, event_type: str, candidate: Candidate, summary: str) -> None:
        if self.receipt_store is None:
            return
        self.receipt_store.append(
            "candidate_event",
            step,
            summary,
            {"candidate_event_type": event_type, "candidate": candidate.as_dict()},
        )


def _looks_viable(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _VIABLE_MARKERS) or bool(_CONNECTED_LINE_RE.search(text or ""))


def _ports_from_text(text: str) -> set[str]:
    return {match.group("port") for match in _PORT_RE.finditer(text or "")}


def _evidence_summary(tool_name: str, text: str) -> str:
    compact = " ".join((text or "").split())
    return f"{tool_name}: {compact[:300]}"


def _looks_like_probe_client(command: str) -> bool:
    compact = " ".join((command or "").strip().lower().split())
    if not compact:
        return False
    return bool(
        re.search(r"(^|[;&|]\s*|\b)(telnet|nc|netcat|socat|curl)\s+(127\.0\.0\.1|localhost)\b", compact)
    )


__all__ = ["Candidate", "CandidatePreservation"]
