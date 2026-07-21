"""Failure-card generation for Aether/Harbor result rows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import re

FAILURE_CLASSES = frozenset({
    "VALID_CLEAN",
    "VALID_REPAIRED",
    "BLOCKED_HARNESS",
    "ENV_INFRA",
    "HARNESS_TOOLING",
    "VERIFIER_WIRING",
    "CONTEXT_POLICY",
    "ARTIFACT_INSPECTION",
    "TASK_LOCAL_TOOLING",
    "SERVICE_MONITORING",
    "MODEL_CAPABILITY",
    "PROVIDER_POLICY_REFUSAL",
    "AHP_PROMPT_FRAMING_REFUSAL",
    "MODEL_REFUSAL",
    "UNKNOWN",
})

_REFUSAL_RE = re.compile(
    r"\b(?:can't|cannot|can not|won't|will not|unable to)\b.{0,160}\b(?:policy|unsafe|harmful|xss|bypass|exploit|malware|credential|illicit)\b",
    re.IGNORECASE | re.DOTALL,
)


def classify_failure(row: Mapping[str, Any], status: Mapping[str, Any], receipt_events: list[dict[str, Any]] | None = None) -> str:
    reward = row.get("mean")
    summary = str(status.get("summary", ""))
    if reward == 1.0:
        return "VALID_REPAIRED" if int(status.get("transcript_repairs") or 0) else "VALID_CLEAN"
    if _REFUSAL_RE.search(summary):
        return "PROVIDER_POLICY_REFUSAL"
    events_blob = json.dumps(receipt_events or [], sort_keys=True, ensure_ascii=True).lower()
    if "unsupported_binary" in events_blob or "artifact_observation" in events_blob and "not_found" in events_blob:
        return "ARTIFACT_INSPECTION"
    if "verification blocked" in events_blob:
        return "VERIFIER_WIRING" if status.get("verifier_readiness") is False else "MODEL_CAPABILITY"
    if "connection closed by foreign host" in events_blob or "listening" in events_blob:
        return "SERVICE_MONITORING"
    if status.get("reason_code") not in {None, "harbor_loop_finished"}:
        return "BLOCKED_HARNESS"
    return "MODEL_CAPABILITY" if reward == 0.0 else "UNKNOWN"


def build_failure_card(row_bundle: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(row_bundle.get("row") or {})
    status = dict(row_bundle.get("status") or {})
    events = list(row_bundle.get("receipt_events") or [])
    exchanges = list(row_bundle.get("model_exchanges") or [])
    first_normal = next((item for item in exchanges if item.get("call_role") == "normal"), {})
    policy = (row_bundle.get("ahp_profile") or {}).get("context_pack_policy") if isinstance(row_bundle.get("ahp_profile"), Mapping) else None
    verifier_feedback = [event for event in events if event.get("event_type") == "verification_feedback"]
    plan_updates = [event for event in events if event.get("event_type") == "plan_update"]
    local_tools = [event for event in events if event.get("event_type") == "task_local_tools"]
    artifact_observations = [event for event in events if event.get("event_type") == "artifact_observation"]
    task_done_events = [
        action for action in row_bundle.get("actions", [])
        if action.get("tool") == "task_done"
    ]
    query_events = [
        action for action in row_bundle.get("actions", [])
        if action.get("tool") == "query_evidence"
    ]
    failure_class = classify_failure(row, status, events)
    return {
        "task": row.get("task"),
        "condition": row.get("condition"),
        "rep": row.get("rep"),
        "reward": row.get("mean"),
        "status": row.get("aether_status") or status.get("status"),
        "model_calls": row.get("model_calls") or status.get("model_calls"),
        "steps": row.get("steps") or status.get("steps"),
        "transcript_repairs": status.get("transcript_repairs", 0),
        "verifier_readiness": status.get("verifier_readiness"),
        "finalize_reason": row.get("finalize_reason") or status.get("finalize_reason"),
        "system_prompt_digest": first_normal.get("system_prompt_digest") or _digest(first_normal.get("system_prompt_prefix", "")),
        "context_policy_summary": _policy_summary(policy),
        "missing_evidence": _missing_evidence(events, status),
        "open_requirements": _open_requirements(events),
        "verifier_feedback": [_compact_event(event) for event in verifier_feedback[-3:]],
        "plan_updates": [_compact_event(event) for event in plan_updates[-3:]],
        "task_done_events": task_done_events[-3:],
        "local_tools": local_tools[-3:],
        "artifact_observations": [_compact_event(event) for event in artifact_observations[-5:]],
        "query_evidence": query_events[-5:],
        "provider_refusal": failure_class in {"PROVIDER_POLICY_REFUSAL", "AHP_PROMPT_FRAMING_REFUSAL", "MODEL_REFUSAL"},
        "primary_failure_class": failure_class,
        "secondary_factors": _secondary_factors(row_bundle, failure_class),
        "recommended_responsible_layer": _responsible_layer(failure_class),
        "evidence_paths": {
            "result_path": row.get("result_path"),
            "status_path": row_bundle.get("status_path") or row.get("status_path"),
        },
    }


def _digest(text: Any) -> str | None:
    if not text:
        return None
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _policy_summary(policy: Any) -> dict[str, Any] | None:
    if not isinstance(policy, Mapping):
        return None
    return {
        "include_sections": policy.get("include_sections"),
        "always_include": policy.get("always_include"),
        "full_previous_steps": policy.get("full_previous_steps"),
        "receipt_event_budget": policy.get("receipt_event_budget"),
    }


def _compact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "step": event.get("step"),
        "summary": str(event.get("summary", ""))[:500],
    }


def _missing_evidence(events: list[dict[str, Any]], status: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        for item in payload.get("missing_external_state", []) if isinstance(payload.get("missing_external_state"), list) else []:
            missing.append(str(item))
    summary = str(status.get("summary", ""))
    if "not yet" in summary.lower() or "couldn't" in summary.lower():
        missing.append(summary[:500])
    return missing[:8]


def _open_requirements(events: list[dict[str, Any]]) -> list[str]:
    for event in reversed(events):
        if event.get("event_type") == "success_contract":
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            return [str(item) for item in payload.get("verbatim_lines", [])][:12]
    return []


def _secondary_factors(row_bundle: Mapping[str, Any], failure_class: str) -> list[str]:
    factors: list[str] = []
    events_blob = json.dumps(row_bundle.get("receipt_events") or [], sort_keys=True, ensure_ascii=True).lower()
    if "query_evidence" in events_blob:
        factors.append("query_evidence_used")
    if "task_blocked" in events_blob:
        factors.append("explicit_blocker_reported")
    if failure_class == "SERVICE_MONITORING":
        factors.append("active_candidate_lifecycle")
    return factors


def _responsible_layer(failure_class: str) -> str:
    return {
        "PROVIDER_POLICY_REFUSAL": "provider/model policy",
        "AHP_PROMPT_FRAMING_REFUSAL": "AHP prompt framing",
        "SERVICE_MONITORING": "service lifecycle / candidate preservation",
        "VERIFIER_WIRING": "verifier policy",
        "CONTEXT_POLICY": "context pack policy",
        "ARTIFACT_INSPECTION": "artifact inspection",
        "TASK_LOCAL_TOOLING": "task-local tool trust gate",
        "BLOCKED_HARNESS": "harness/runner",
    }.get(failure_class, "model capability / task strategy")


def write_failure_cards(row_bundles: list[Mapping[str, Any]], output_path: Path) -> list[dict[str, Any]]:
    cards = [build_failure_card(bundle) for bundle in row_bundles]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(json.dumps(card, sort_keys=True, ensure_ascii=True) for card in cards) + "\n", encoding="utf-8")
    return cards


__all__ = ["FAILURE_CLASSES", "build_failure_card", "classify_failure", "write_failure_cards"]
