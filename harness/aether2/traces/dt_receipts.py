"""Receipt extraction and JSON summary utilities for decision trace extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from harness.aether2.traces.dt_row_loading import (
    _resolved,
    _read_text,
    _loads_json,
    _infer_run_ref,
)

_PASSTHROUGH_METADATA_FIELDS = (
    "persistent_blockers",
    "verifier_suppression_metrics",
    "verifier_suppression",
    "environment_contract_version",
    "environment_contract_digest",
    "environment_contract_ref",
    "reasoning_trace_ref",
)


def summarize_text(value: Any, *, limit: int = 220) -> str:
    """Bounded summarization of text snippets for display."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text
    head = text[: max(0, limit - 24)].rstrip()
    tail = text[-8:].lstrip() if len(text) > 8 else ""
    suffix = f" … [truncated {len(text) - len(head) - len(tail)} chars]"
    return f"{head}{suffix}{tail}"


def _short_json(value: Any, *, limit: int = 4) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        out = [_short_json(item, limit=limit) for item in value[:limit]]
        if len(value) > limit:
            out.append({"truncated_items": len(value) - limit})
        return out
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in sorted(value.keys())[:limit]:
            out[str(key)] = _short_json(value[key], limit=limit)
        if len(value) > limit:
            out["_truncated_keys"] = len(value) - limit
        return out
    return summarize_text(str(value), limit=120)


def _dedupe_dicts(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        marker = json.dumps(item, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


def _extract_embedded_tool_invocations(row: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(row, dict):
        return []
    for outer_key in ("run_result", "loop_result"):
        outer = row.get(outer_key)
        if not isinstance(outer, dict):
            continue
        invocations = outer.get("tool_invocations")
        if isinstance(invocations, list) and invocations:
            return [item for item in invocations if isinstance(item, dict)]
    invocations = row.get("tool_invocations")
    if isinstance(invocations, list):
        return [item for item in invocations if isinstance(item, dict)]
    return []


def _extract_row_discrepancy_reports(row: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(row, dict):
        return []
    for outer_key in ("run_result", "loop_result"):
        outer = row.get(outer_key)
        if isinstance(outer, dict):
            reports = outer.get("discrepancy_reports")
            if isinstance(reports, list):
                return [item for item in reports if isinstance(item, dict)]
    reports = row.get("discrepancy_reports")
    if isinstance(reports, list):
        return [item for item in reports if isinstance(item, dict)]
    return []


def _load_json_receipt(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    text, issues = _read_text(path)
    if text is None:
        return None, issues
    payload, parse_issues = _loads_json(text, source_ref=str(path))
    if isinstance(payload, dict):
        return payload, issues + parse_issues
    return None, issues + parse_issues + [f"{path}: expected a JSON object"]


def _extract_external_receipt_paths(
    row: dict[str, Any] | None,
    row_bundle: dict[str, Any],
    *,
    source_input_ref: str,
    source_row_ref: str,
) -> list[Path]:
    refs: list[str] = []
    inferred_run = _infer_run_ref(Path(source_input_ref), row, source_row_ref)
    inferred_run_path = inferred_run.get("run_path")
    if inferred_run_path:
        run_dir = Path(inferred_run_path)
        for context_path in sorted((run_dir / "verifier_context").glob("*.json")):
            refs.append(str(context_path))
    if isinstance(row, dict):
        for key in ("trace_refs", "artifact_refs"):
            value = row.get(key)
            if isinstance(value, list):
                refs.extend(str(item) for item in value if isinstance(item, (str, Path)) and str(item))
        for key in ("route_trace_ref", "verifier_ref", "grader_ref", "environment_ref", "reasoning_trace_ref", "result_ref"):
            value = row.get(key)
            if isinstance(value, str) and value:
                refs.append(value)
        if isinstance(row.get("artifacts"), str):
            artifact_path = Path(str(row["artifacts"]))
            if artifact_path.is_dir():
                for child_name in (
                    "artifact_bundle.json",
                    "grader_output.json",
                    "verifier_output.json",
                    "environment_contract.json",
                    "environment_manifest.json",
                    "service_evidence.json",
                    "aether2_result.json",
                    "grader_isolation_contract.json",
                ):
                    child = artifact_path / child_name
                    if child.exists():
                        refs.append(str(child))
            elif artifact_path.exists():
                refs.append(str(artifact_path))
        if isinstance(row.get("run_dir"), str):
            run_dir = Path(str(row["run_dir"]))
            for candidate in (
                run_dir / "route_trace" / "run_events.jsonl",
                run_dir / "route_trace" / "run_header.json",
                run_dir / "route_trace" / "score_envelope.json",
            ):
                if candidate.exists():
                    refs.append(str(candidate))
    for entry in row_bundle.get("receipt_bundle", []):
        if isinstance(entry, dict):
            ref = entry.get("receipt_ref")
            if isinstance(ref, str) and ref:
                refs.append(ref.split("#", 1)[0])
    unique: list[Path] = []
    seen: set[str] = set()
    for ref in refs:
        path = Path(ref)
        resolved = _resolved(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _known_json_summary_keys(filename: str) -> tuple[str, ...]:
    if filename == "artifact_bundle.json":
        return ("authority_label", "environment_manifest_ref", "grader_ref", "route_trace_ref", "trace_refs", "verifier_ref")
    if filename == "verifier_output.json":
        return ("benchmark_case_id", "returncode", "reward", "reward_path", "status", "stderr_tail", "stdout_tail")
    if filename == "grader_output.json":
        return ("benchmark_case_id", "failure_class", "reason_codes", "score", "verdict")
    if filename == "environment_manifest.json":
        return ("certification_mode", "container_workspace_path", "initial_cwd", "host_fixture_root")
    if filename == "run_header.json":
        return ("run_id", "task_id", "workspace_root")
    if filename == "score_envelope.json":
        return ("score", "reason_codes", "verdict")
    if filename == "trace.json":
        return ("events", "meta")
    if filename == "reasoning_trace.json":
        return ("schema_version", "step_count", "finalize_reason", "verifier_clean", "steps")
    return ()


def _summarize_json_receipt(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "receipt_ref": _resolved(path),
        "receipt_kind": "json_receipt",
        "receipt_name": path.name,
    }
    for key in _known_json_summary_keys(path.name):
        if key in payload:
            summary[key] = _short_json(payload[key], limit=4)
    if path.name not in {"artifact_bundle.json", "verifier_output.json", "grader_output.json", "environment_manifest.json", "run_header.json", "score_envelope.json", "trace.json"}:
        scalar_keys = [key for key in sorted(payload.keys()) if isinstance(payload[key], (str, int, float, bool))][:6]
        for key in scalar_keys:
            summary[key] = payload[key]
    return summary


def _collect_passthrough_metadata(*payloads: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for field in _PASSTHROUGH_METADATA_FIELDS:
            value = payload.get(field)
            if value is not None and field not in metadata:
                metadata[field] = value
        for outer_key in ("run_result", "loop_result"):
            outer = payload.get(outer_key)
            if not isinstance(outer, dict):
                continue
            for field in _PASSTHROUGH_METADATA_FIELDS:
                value = outer.get(field)
                if value is not None and field not in metadata:
                    metadata[field] = value
    return metadata
