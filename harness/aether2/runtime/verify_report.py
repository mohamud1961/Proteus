"""Report parsing, normalization, and response-extraction helpers for verify.py.

Extracted from verify.py to keep that module under 500 LOC.
These are internal helpers; callers should import from verify.py.
"""

from __future__ import annotations

import json

from harness.aether2.runtime.context import sanitize_model_visible_payload
import re
from typing import Any, Mapping

from harness.aether2.runtime.verify_evidence import _dedupe

__all__ = [
    "_CONSTRAINT_SIGNAL_TOKENS",
    "_assistant_message",
    "_build_evidence_source_catalog",
    "_call_model",
    "_constraint_coverage_tokens",
    "_extract_text",
    "_extract_tool_calls",
    "_inspection_payload",
    "_looks_like_constraint",
    "_normalize_requirement_item",
    "_normalize_verdict",
    "_parse_report",
    "_parse_tool_call_arguments",
    "_read_error_field",
    "_read_field",
    "_strip_transcript_fields",
    "_tool_call_name",
    "_verifier_output_failure",
]

# W5.2: only stated requirements that read as constraints, final-state, or
# side-effect/path requirements are checked for verifier coverage.
_CONSTRAINT_SIGNAL_TOKENS = (
    "not ",
    "must not",
    "only",
    "without",
    "forbidden",
    "must remain",
    "do not",
    "don't",
    "never",
    "final",
    "remain unchanged",
    "preserve",
    "outside",
    "/",
)


def _normalize_verdict(verdict: str) -> str:
    normalized = verdict.strip().lower()
    if normalized in {"satisfied", "unsatisfied", "unverifiable"}:
        return normalized
    return "unverifiable"


def _strip_transcript_fields(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if "transcript" in str(key).lower():
                continue
            cleaned[str(key)] = _strip_transcript_fields(value)
        return cleaned
    if isinstance(payload, list):
        return [_strip_transcript_fields(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(_strip_transcript_fields(item) for item in payload)
    return payload


def _call_model(model_client: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
    if hasattr(model_client, "call"):
        return model_client.call(messages, tools, cache_prefix_len=0)
    raise TypeError("model_client must define call(messages, tools, *, cache_prefix_len)")


def _read_field(raw: Any, name: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(name, default)
    return getattr(raw, name, default)


def _read_error_field(raw: Any, name: str) -> str | None:
    error = _read_field(raw, "error", None)
    if error is None:
        return None
    if isinstance(error, Mapping):
        value = error.get(name)
    else:
        value = getattr(error, name, None)
    if value is None:
        return None
    return str(value)


def _extract_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        for key in ("output_text", "text", "content"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
    if hasattr(response, "output_text") and isinstance(response.output_text, str):
        return response.output_text
    if hasattr(response, "text") and isinstance(response.text, str):
        return response.text
    if hasattr(response, "content") and isinstance(response.content, str):
        return response.content
    return json.dumps(response, sort_keys=True, default=str, ensure_ascii=True)


def _extract_tool_calls(response: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(response, Mapping):
        tool_calls = response.get("tool_calls")
        if isinstance(tool_calls, list):
            return tuple(dict(item) for item in tool_calls if isinstance(item, Mapping))
    value = getattr(response, "tool_calls", ())
    if isinstance(value, tuple):
        return tuple(dict(item) for item in value if isinstance(item, Mapping))
    if isinstance(value, list):
        return tuple(dict(item) for item in value if isinstance(item, Mapping))
    return ()


def _assistant_message(response: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": _extract_text(response)}
    tool_calls = _extract_tool_calls(response)
    if tool_calls:
        message["tool_calls"] = [dict(item) for item in tool_calls]
    return message


def _tool_call_name(tool_call: Mapping[str, Any]) -> str | None:
    name = tool_call.get("name")
    if isinstance(name, str) and name:
        return name
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        nested_name = function.get("name")
        if isinstance(nested_name, str) and nested_name:
            return nested_name
    return None


def _parse_tool_call_arguments(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    arguments = tool_call.get("arguments")
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _inspection_payload(result: Any) -> dict[str, Any]:
    payload = {
        "exit_code": _read_field(result, "exit_code"),
        "cwd": _read_field(result, "cwd", ""),
        "stdout_head": _read_field(result, "stdout_head", _read_field(result, "stdout", "")),
        "stdout_tail": _read_field(result, "stdout_tail", ""),
        "stderr_head": _read_field(result, "stderr_head", _read_field(result, "stderr", "")),
        "stderr_tail": _read_field(result, "stderr_tail", ""),
    }
    error = _read_field(result, "error", None)
    if error is not None:
        if isinstance(error, Mapping):
            payload["error"] = dict(error)
        else:
            payload["error"] = getattr(error, "__dict__", {"message": str(error)})
    return sanitize_model_visible_payload(payload)


def _verifier_output_failure(
    *,
    requirement: str,
    evidence: str,
    reason_codes: tuple[str, ...],
    summary: str,
    evidence_refs: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "requirements": [
            {
                "requirement": requirement,
                "verdict": "unverifiable",
                "evidence": evidence,
                "evidence_refs": list(evidence_refs),
            }
        ],
        "reason_codes": list(reason_codes),
        "summary": summary,
    }


def _normalize_requirement_item(item: Any, *, index: int) -> tuple[dict[str, Any] | None, list[str]]:
    label = f"requirements[{index}]"
    if not isinstance(item, Mapping):
        return None, [f"{label} must be an object"]
    requirement = item.get("requirement")
    evidence = item.get("evidence")
    verdict = item.get("verdict")
    raw_evidence_refs = item.get("evidence_refs")
    issues: list[str] = []
    if not isinstance(requirement, str) or not requirement.strip():
        issues.append(f"{label}.requirement must be a non-empty string")
    if not isinstance(evidence, str) or not evidence.strip():
        issues.append(f"{label}.evidence must be a non-empty string")
    if not isinstance(verdict, str) or _normalize_verdict(verdict) != verdict.strip().lower():
        issues.append(f'{label}.verdict must be "satisfied", "unsatisfied", or "unverifiable"')
    evidence_refs: list[str] = []
    if raw_evidence_refs is None:
        issues.append(f"{label}.evidence_refs must be a list")
    elif not isinstance(raw_evidence_refs, list):
        issues.append(f"{label}.evidence_refs must be a list")
    else:
        evidence_refs = [str(ref) for ref in raw_evidence_refs if str(ref).strip()]
        if not evidence_refs:
            issues.append(f"{label}.evidence_refs must contain at least one non-empty reference")
    if not isinstance(requirement, str) or not requirement.strip():
        return None, issues
    normalized = {
        "requirement": requirement.strip(),
        "verdict": _normalize_verdict(str(verdict if verdict is not None else "")),
        "evidence": str(evidence if evidence is not None else ""),
        "evidence_refs": evidence_refs,
    }
    return normalized, issues


def _parse_report(raw_text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except Exception:
        return _verifier_output_failure(
            requirement="verification_output_parse",
            evidence="Verifier output was not valid JSON for the required verification schema.",
            reason_codes=("verifier_parse_failed", "verifier_output_not_json"),
            summary="Verifier output could not be parsed.",
            evidence_refs=("verifier.raw_response",),
        )
    if not isinstance(parsed, dict):
        return _verifier_output_failure(
            requirement="verification_output_schema",
            evidence="Verifier output JSON was not an object matching the required top-level schema.",
            reason_codes=("verifier_parse_failed", "verifier_schema_invalid"),
            summary="Verifier output could not be normalized to the required schema.",
            evidence_refs=("verifier.raw_response", "verifier.parsed_output"),
        )
    if "requirements" not in parsed or "reason_codes" not in parsed or "summary" not in parsed:
        return _verifier_output_failure(
            requirement="verification_output_schema",
            evidence="Verifier output JSON was missing one or more required top-level keys: requirements, reason_codes, summary.",
            reason_codes=("verifier_parse_failed", "verifier_schema_invalid"),
            summary="Verifier output could not be normalized to the required schema.",
            evidence_refs=("verifier.raw_response", "verifier.parsed_output"),
        )
    requirements = parsed.get("requirements", [])
    reason_codes = parsed.get("reason_codes", [])
    summary = parsed.get("summary", "")
    if not isinstance(requirements, list) or not isinstance(reason_codes, list) or not isinstance(summary, str):
        return _verifier_output_failure(
            requirement="verification_output_schema",
            evidence="Verifier output JSON used invalid top-level value types for requirements, reason_codes, or summary.",
            reason_codes=("verifier_parse_failed", "verifier_schema_invalid"),
            summary="Verifier output could not be normalized to the required schema.",
            evidence_refs=("verifier.raw_response", "verifier.parsed_output"),
        )
    normalized_requirements: list[dict[str, Any]] = []
    schema_issues: list[str] = []
    for index, item in enumerate(requirements):
        normalized_item, item_issues = _normalize_requirement_item(item, index=index)
        schema_issues.extend(item_issues)
        if normalized_item is not None:
            normalized_requirements.append(normalized_item)
    if schema_issues:
        normalized_requirements.append(
            {
                "requirement": "verification_output_schema",
                "verdict": "unverifiable",
                "evidence": (
                    "Verifier output violated the required requirement schema: "
                    + "; ".join(schema_issues[:4])
                ),
                "evidence_refs": ["verifier.raw_response", "verifier.parsed_output"],
            }
        )
        reason_codes = _dedupe([*(str(item) for item in reason_codes), "verifier_parse_failed", "verifier_schema_invalid"])
    else:
        reason_codes = [str(item) for item in reason_codes]
    parsed["requirements"] = normalized_requirements
    parsed["reason_codes"] = reason_codes
    parsed["summary"] = summary
    return parsed


def _build_evidence_source_catalog(
    orientation: Mapping[str, Any],
    diff: Mapping[str, Any],
    claim: Mapping[str, Any],
    checks_results: list[Mapping[str, Any]],
    action_digest: Mapping[str, Any],
    inspection_records: list[Mapping[str, Any]],
    *,
    raw_response: str,
    parsed_report: Mapping[str, Any],
) -> dict[str, Any]:
    catalog: dict[str, Any] = {
        "orientation": orientation,
        "workspace_diff": diff,
        "claim": claim,
        "checks_results": checks_results,
        "action_digest": action_digest,
        "verifier.raw_response": raw_response,
        "verifier.parsed_output": parsed_report,
    }
    tool_calls = action_digest.get("tool_calls")
    if isinstance(tool_calls, list):
        catalog["action_digest.tool_calls"] = tool_calls
        for index, tool_call in enumerate(tool_calls):
            catalog[f"action_digest.tool_calls[{index}]"] = tool_call
    for index, result in enumerate(checks_results):
        catalog[f"checks_results[{index}]"] = result
    inspection_by_tool: dict[str, list[Mapping[str, Any]]] = {}
    for record in inspection_records:
        tool_name = str(record.get("tool_name", "")).strip()
        if not tool_name:
            continue
        inspection_by_tool.setdefault(tool_name, []).append(record)
    for tool_name, records in inspection_by_tool.items():
        catalog[f"inspection.{tool_name}"] = records
        for index, record in enumerate(records):
            catalog[f"inspection.{tool_name}[{index}]"] = record
    return catalog


def _constraint_coverage_tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 4}


def _looks_like_constraint(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in _CONSTRAINT_SIGNAL_TOKENS)
