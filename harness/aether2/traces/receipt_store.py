"""Queryable receipt store for receipt-driven Aether variants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
import json
import re
import time

from harness.aether2.runtime.context import sanitize_model_visible_payload
from harness.aether2.runtime.run_config import ContextPackPolicy


@dataclass(frozen=True)
class ReceiptEvent:
    event_id: str
    event_type: str
    step: int | None
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "step": self.step,
            "summary": self.summary,
            "payload": self.payload,
            "created_at": self.created_at,
        }


class QueryableReceiptStore:
    """Append-only current-run event log with a bounded model-visible view."""

    def __init__(self, *, root: Path, run_id: str = "aether_receipt_variant") -> None:
        self.root = root
        self.run_id = run_id
        self.store_dir = root / ".aether2" / "receipt_store"
        self.events_path = self.store_dir / "events.jsonl"
        self.plan_path = self.store_dir / "plan.json"
        self.contract_path = self.store_dir / "success_contract.json"
        self.operating_contract_path = self.store_dir / "task_operating_contract.json"
        self._events: list[ReceiptEvent] = []
        self._next_id = 1
        self._plan: dict[str, Any] = {"version": 0, "items": [], "last_update": ""}
        self._success_contract: dict[str, Any] = {}
        self._task_operating_contract: dict[str, Any] = {}
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def set_success_contract(self, contract: Mapping[str, Any]) -> None:
        self._success_contract = _jsonable(dict(contract))
        self.contract_path.write_text(
            json.dumps(self._success_contract, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        self.append("success_contract", None, "success contract recorded", self._success_contract)

    def set_task_operating_contract(self, contract: Mapping[str, Any], *, step: int | None) -> None:
        self._task_operating_contract = _jsonable(dict(contract))
        self.operating_contract_path.write_text(
            json.dumps(self._task_operating_contract, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        self.append("task_operating_contract", step, "task operating contract recorded", self._task_operating_contract)

    def task_operating_contract(self) -> dict[str, Any]:
        return dict(self._task_operating_contract)

    def update_plan(self, *, step: int | None, plan_text: str | None, reason: str) -> None:
        text = (plan_text or "").strip()
        if not text or text == self._plan.get("last_update"):
            return
        self._plan = {
            "version": int(self._plan.get("version", 0)) + 1,
            "items": parse_plan_update(text, prior_items=self._plan.get("items", [])),
            "last_update": text,
            "reason": reason,
            "step": step,
        }
        self.plan_path.write_text(
            json.dumps(self._plan, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        self.append("plan_update", step, "plan updated", self._plan)

    def append(
        self,
        event_type: str,
        step: int | None,
        summary: str,
        payload: Mapping[str, Any] | None = None,
    ) -> ReceiptEvent:
        event = ReceiptEvent(
            event_id=f"evt_{self._next_id:05d}",
            event_type=event_type,
            step=step,
            summary=_clip(str(summary), 1000),
            payload=_jsonable(dict(payload or {})),
        )
        self._next_id += 1
        self._events.append(event)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
        return event

    def record_model_decision(
        self,
        *,
        step: int,
        text: str,
        tool_calls: Iterable[Mapping[str, Any]] | None,
        plan_text: str | None,
    ) -> None:
        tool_names: list[str] = []
        for call in tool_calls or []:
            func = call.get("function") if isinstance(call, Mapping) else None
            if isinstance(func, Mapping) and func.get("name"):
                tool_names.append(str(func["name"]))
        self.append(
            "model_decision",
            step,
            _clip(text.strip() or "model emitted tool calls", 1200),
            {"tool_names": tool_names, "plan_text": plan_text or ""},
        )
        self.update_plan(step=step, plan_text=plan_text, reason="model_decision")

    def record_tool_result(
        self,
        *,
        step: int,
        tool_name: str,
        arguments: Mapping[str, Any],
        exit_code: int | None,
        stdout: str,
        stderr: str,
        raw_log_path: str | None,
        files_changed: list[str],
    ) -> ReceiptEvent:
        status = "passed" if exit_code == 0 else "failed"
        return self.append(
            "tool_result",
            step,
            f"{tool_name} {status}: {_clip(stdout or stderr, 500)}",
            {
                "tool_name": tool_name,
                "arguments": _summarize_arguments(arguments),
                "exit_code": exit_code,
                "stdout_excerpt": _clip(stdout, 1000),
                "stderr_excerpt": _clip(stderr, 1000),
                "raw_log_path": raw_log_path,
                "files_changed": files_changed,
            },
        )

    def record_verification_feedback(self, *, step: int | None, ready: bool, feedback: Mapping[str, Any] | str) -> None:
        if isinstance(feedback, Mapping):
            summary = json.dumps(feedback, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            payload = dict(feedback)
        else:
            summary = str(feedback)
            payload = {"feedback": summary}
        self.append(
            "verification_feedback",
            step,
            ("verification ready" if ready else "verification blocked") + ": " + _clip(summary, 800),
            {"ready": ready, **payload},
        )

    def record_run_telemetry(
        self,
        *,
        step: int | None,
        model_calls: int,
        tokens_cached: int,
        tokens_fresh: int,
        latency_sec: float,
        no_progress_streak: int,
        proof_state_delta: int | None,
        cost_usd: float | None = None,
        proof_state: Mapping[str, Any] | None = None,
        rejected_proxy_evidence: list[str] | None = None,
    ) -> None:
        payload = {
            "model_calls": int(model_calls),
            "tokens_cached": int(tokens_cached),
            "tokens_fresh": int(tokens_fresh),
            "latency_sec": round(float(latency_sec), 3),
            "no_progress_streak": int(no_progress_streak),
            "proof_state_delta": proof_state_delta,
            "proof_state": dict(proof_state) if isinstance(proof_state, Mapping) else proof_state,
            "rejected_proxy_evidence": rejected_proxy_evidence or [],
        }
        if cost_usd is not None:
            payload["cost_usd"] = round(float(cost_usd), 6)
        self.append(
            "run_telemetry",
            step,
            (
                f"run telemetry cached={payload['tokens_cached']} fresh={payload['tokens_fresh']} "
                f"latency={payload['latency_sec']}s no_progress={payload['no_progress_streak']}"
            ),
            payload,
        )

    def record_artifact_observation(
        self,
        *,
        step: int | None,
        path: str,
        mode: str,
        status: str,
        summary: str,
        payload: Mapping[str, Any] | None = None,
    ) -> ReceiptEvent:
        return self.append(
            "artifact_observation",
            step,
            summary,
            {"path": path, "mode": mode, "status": status, **dict(payload or {})},
        )

    def query(self, query: str, *, event_type: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        needle = query.lower().strip()
        out: list[dict[str, Any]] = []
        for event in reversed(self._events):
            if event_type and event.event_type != event_type:
                continue
            blob = json.dumps(event.as_dict(), sort_keys=True, ensure_ascii=True).lower()
            if needle and needle not in blob:
                continue
            out.append(event.as_dict())
            if len(out) >= max(1, min(int(limit), 50)):
                break
        return out

    def known_event_ids(self) -> set[str]:
        return {event.event_id for event in self._events}

    def events(self, *, event_type: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        rows = [
            event.as_dict()
            for event in self._events
            if event_type is None or event.event_type == event_type
        ]
        if limit is None:
            return rows
        return rows[-max(0, int(limit)) :]

    def context_view(
        self,
        *,
        policy: ContextPackPolicy,
        local_tools: Mapping[str, Any] | None = None,
        proof_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        sections = set(policy.include_sections) | set(policy.always_include)
        payload: dict[str, Any] = {"run_id": "current_run", "event_count": len(self._events)}
        if "success_contract" in sections:
            payload["success_contract"] = _model_visible(self._success_contract)
        if "task_operating_contract" in sections:
            payload["task_operating_contract"] = _model_visible(self._task_operating_contract)
        if "current_plan" in sections:
            payload["plan"] = _model_visible(self._plan)
        if "recent_steps" in sections:
            payload["recent_events"] = [_model_visible_event(event) for event in self._events[-policy.receipt_event_budget:]]
            payload["full_previous_steps"] = [_model_visible(event) for event in self._events_by_step(policy.full_previous_steps)]
        if "recent_failures" in sections:
            payload["recent_failures"] = [_model_visible(event) for event in self._failure_events(policy.failure_event_budget)]
        if "verifier_feedback" in sections:
            payload["verifier_feedback"] = [
                _model_visible_event(event)
                for event in self._events
                if event.event_type == "verification_feedback"
            ][-policy.verifier_feedback_budget :]
        if "task_local_tools" in sections:
            payload["local_tools"] = _model_visible(dict(local_tools or {}))
        if "artifact_observations" in sections:
            payload["artifact_observations"] = [
                _model_visible_event(event)
                for event in self._events
                if event.event_type == "artifact_observation"
            ][-policy.artifact_observation_budget :]
        if "active_jobs" in sections:
            payload["active_candidates"] = [
                _model_visible_event(event)
                for event in self._events
                if event.event_type == "candidate_event"
            ][-policy.receipt_event_budget :]
        if "evidence_refs" in sections:
            payload["evidence_refs"] = _model_visible(_evidence_refs(self._events, policy.tool_result_budget))
        if isinstance(proof_state, Mapping) and proof_state:
            payload["proof_state"] = _model_visible(dict(proof_state))
            payload["proof_state_delta"] = proof_state.get("delta")
            payload["rejected_proxy_evidence"] = _model_visible(
                [str(item) for item in list(proof_state.get("rejected_proxy_evidence", []) or []) if str(item).strip()]
            )
        return payload

    def _events_by_step(self, step_count: int) -> list[dict[str, Any]]:
        step_ids = sorted({event.step for event in self._events if isinstance(event.step, int)})
        selected = set(step_ids[-max(0, step_count):])
        return [event.as_dict() for event in self._events if event.step in selected]

    def _failure_events(self, limit: int) -> list[dict[str, Any]]:
        failures = [
            event.as_dict()
            for event in self._events
            if event.event_type in {"verification_feedback", "tool_result"}
            and ("blocked" in event.summary.lower() or "failed" in event.summary.lower())
        ]
        return failures[-max(0, limit):]


def _split_plan_items(text: str) -> list[str]:
    return [line.strip(" -\t") for line in text.splitlines() if line.strip()][:12]


_PLAN_STATUS_ALIASES = {
    "todo": "pending",
    "next": "pending",
    "doing": "in_progress",
    "in-progress": "in_progress",
    "complete": "done",
    "completed": "done",
}
_PLAN_STATUSES = frozenset({"pending", "in_progress", "done", "blocked", "superseded"})
_PLAN_UPDATE_RE = re.compile(
    r"(?im)^\s*PLAN_UPDATE:\s*$"
    r"(?P<body>(?:\n\s*-\s*\[[^\]]+\]\s+.+)+)"
)
_PLAN_ITEM_RE = re.compile(r"^\s*-\s*\[(?P<status>[^\]]+)\]\s+(?P<text>.+?)\s*$")


def parse_plan_update(text: str, *, prior_items: Any = None) -> list[dict[str, Any]]:
    """Parse lightweight PLAN_UPDATE checkoffs, preserving prior items when absent."""
    prior = [dict(item) for item in prior_items or [] if isinstance(item, Mapping)]
    match = _PLAN_UPDATE_RE.search(text or "")
    if match is None:
        if prior:
            return prior
        return [
            {
                "status": "pending",
                "text": line,
                "evidence_refs": [],
                "evidence_missing": True,
                "source": "unstructured_plan_text",
            }
            for line in _split_plan_items(text)
        ]

    indexed = {
        str(item.get("text", "")).strip(): item
        for item in prior
        if str(item.get("text", "")).strip()
    }
    ordered: list[dict[str, Any]] = []
    for line in match.group("body").splitlines():
        item_match = _PLAN_ITEM_RE.match(line)
        if item_match is None:
            continue
        raw_status = item_match.group("status").strip().lower().replace(" ", "_")
        status = _PLAN_STATUS_ALIASES.get(raw_status, raw_status)
        if status not in _PLAN_STATUSES:
            status = "pending"
        item_text = " ".join(item_match.group("text").split())
        previous = dict(indexed.pop(item_text, {}))
        evidence_refs = list(previous.get("evidence_refs") or [])
        ordered.append(
            {
                **previous,
                "status": status,
                "text": item_text,
                "evidence_refs": evidence_refs,
                "evidence_missing": status == "done" and not evidence_refs,
                "source": "PLAN_UPDATE",
            }
        )
    ordered.extend(indexed.values())
    return ordered[:24]


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _jsonable(payload: Any) -> Any:
    return json.loads(json.dumps(payload, sort_keys=True, default=str))


def _summarize_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _clip(str(value), 500) for key, value in arguments.items()}


def _evidence_refs(events: list[ReceiptEvent], limit: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for event in events:
        raw_ref = event.payload.get("raw_log_path") if isinstance(event.payload, dict) else None
        if raw_ref:
            refs.append(
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "step": event.step,
                    "summary": event.summary,
                    "raw_log_available": True,
                }
            )
    return refs[-max(0, limit):]


def _model_visible_event(event: ReceiptEvent) -> dict[str, Any]:
    return _model_visible(event.as_dict())


def _model_visible(payload: Any) -> Any:
    """Redact host/run metadata from model-visible receipt context only."""
    return sanitize_model_visible_payload(payload)


__all__ = ["ReceiptEvent", "QueryableReceiptStore", "parse_plan_update"]
