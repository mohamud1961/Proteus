"""Generic artifact registry helpers for the active kernel."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.aether2.traces.artifact_type_tables import (
    ARTIFACT_TYPE_GUESSES,
    guess_artifact_type,
    _sha256_file,
)
from harness.aether2.traces.artifact_command_classify import (
    classify_artifact_command,
    extract_artifact_path_refs,
    _coerce_artifact_path_ref,
    _invalid_artifact_path_label,
    _dedupe_strings,
    _string_list,
)


@dataclass(frozen=True)
class KernelArtifactRecord:
    """Compact artifact state recorded against a workspace path."""

    path: str
    exists: bool
    size_bytes: int
    sha256: str
    suffix: str
    type_guess: str
    origin_receipt_id: str | None = None
    last_seen_receipt_id: str | None = None
    generated: bool = False
    freshness: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "suffix": self.suffix,
            "type_guess": self.type_guess,
            "origin_receipt_id": self.origin_receipt_id,
            "last_seen_receipt_id": self.last_seen_receipt_id,
            "generated": self.generated,
            "freshness": self.freshness,
        }


def build_artifact_record(
    *,
    path: Path | str,
    workspace_root: Path,
    origin_receipt_id: str | None = None,
    generated: bool | None = None,
) -> dict[str, Any]:
    """Build a stable artifact record for a single visible path."""
    path_ref = _coerce_artifact_path_ref(path)
    if path_ref is None:
        invalid_label = _invalid_artifact_path_label(path)
        record = KernelArtifactRecord(
            path=invalid_label,
            exists=False,
            size_bytes=0,
            sha256="",
            suffix="",
            type_guess="unknown",
            origin_receipt_id=_clean_receipt_id(origin_receipt_id),
            last_seen_receipt_id=_clean_receipt_id(origin_receipt_id),
            generated=bool(generated),
            freshness=_freshness_from_generated(exists=False, generated=generated),
        )
        return record.to_dict()
    path_key, absolute_path = _canonical_artifact_path(Path(path_ref), workspace_root)
    exists = absolute_path.exists()
    suffix_guess = guess_artifact_type(Path(path_key))
    if exists and absolute_path.is_file():
        size_bytes = absolute_path.stat().st_size
        sha256 = _sha256_file(absolute_path)
    else:
        size_bytes = 0
        sha256 = ""
    freshness = _freshness_from_generated(exists=exists, generated=generated)
    record = KernelArtifactRecord(
        path=path_key,
        exists=exists,
        size_bytes=size_bytes,
        sha256=sha256,
        suffix=str(suffix_guess["suffix"] or ""),
        type_guess=str(suffix_guess["type_guess"] or "unknown"),
        origin_receipt_id=_clean_receipt_id(origin_receipt_id),
        last_seen_receipt_id=_clean_receipt_id(origin_receipt_id),
        generated=bool(generated),
        freshness=freshness,
    )
    return record.to_dict()


def refresh_artifact_registry(
    *,
    workspace_root: Path,
    existing: dict[str, dict[str, Any]],
    candidate_paths: list[str],
    receipt_id: str,
) -> dict[str, dict[str, Any]]:
    """Refresh the registry from newly observed candidate paths."""
    refreshed: dict[str, dict[str, Any]] = {}
    normalized_existing = _normalize_existing_registry(existing, workspace_root)
    refreshed.update(normalized_existing)
    for candidate in _dedupe_strings(_string_list(candidate_paths)):
        record = build_artifact_record(path=candidate, workspace_root=workspace_root, generated=True)
        path_key = str(record.get("path") or "")
        if path_key.startswith("<invalid_artifact_path_ref:"):
            continue
        previous = normalized_existing.get(path_key)
        record["origin_receipt_id"] = _clean_receipt_id((previous or {}).get("origin_receipt_id")) or receipt_id
        record["last_seen_receipt_id"] = receipt_id
        if previous:
            previous_sha256 = str(previous.get("sha256") or "")
            current_sha256 = str(record.get("sha256") or "")
            if previous.get("exists") and record.get("exists") and previous_sha256 and current_sha256 and previous_sha256 != current_sha256:
                record["freshness"] = "modified"
            elif not record.get("exists"):
                record["freshness"] = "missing"
        refreshed[path_key] = record
    return refreshed


def summarize_artifact_registry(registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return a compact summary of the artifact registry."""
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(registry, dict):
        for key, record in registry.items():
            if not isinstance(record, dict):
                continue
            path_value = record.get("path")
            normalized_key = str(path_value or key or "")
            if not normalized_key:
                continue
            normalized[normalized_key] = dict(record)
            normalized[normalized_key]["path"] = normalized_key
    records = [_compact_artifact_record(record) for record in normalized.values()]
    records.sort(key=lambda item: (str(item.get("last_seen_receipt_id") or ""), str(item.get("path") or "")))
    type_counts: dict[str, int] = {}
    freshness_counts: dict[str, int] = {}
    original_artifacts: list[str] = []
    generated_artifacts: list[str] = []
    modified_artifacts: list[str] = []
    missing_artifacts: list[str] = []
    unknown_artifacts: list[str] = []
    exists_count = 0
    for record in records:
        type_guess = str(record.get("type_guess") or "unknown")
        freshness = str(record.get("freshness") or "unknown")
        path = str(record.get("path") or "")
        type_counts[type_guess] = type_counts.get(type_guess, 0) + 1
        freshness_counts[freshness] = freshness_counts.get(freshness, 0) + 1
        if bool(record.get("exists")):
            exists_count += 1
        if freshness == "original":
            original_artifacts.append(path)
        elif freshness == "generated":
            generated_artifacts.append(path)
        elif freshness == "modified":
            modified_artifacts.append(path)
        elif freshness == "missing":
            missing_artifacts.append(path)
        else:
            unknown_artifacts.append(path)

    def _truncate_list(lst: list[str], max_len: int = 15) -> list[str]:
        if len(lst) > max_len:
            return lst[:max_len] + [f"... ({len(lst) - max_len} more files omitted)"]
        return lst

    return {
        "artifact_count": len(records),
        "exists_count": exists_count,
        "missing_count": len(missing_artifacts),
        "type_counts": dict(sorted(type_counts.items())),
        "freshness_counts": dict(sorted(freshness_counts.items())),
        "original_artifacts": _truncate_list(original_artifacts),
        "generated_artifacts": _truncate_list(generated_artifacts),
        "modified_artifacts": _truncate_list(modified_artifacts),
        "missing_artifacts": _truncate_list(missing_artifacts),
        "unknown_artifacts": _truncate_list(unknown_artifacts),
        "recent_artifacts": records[-10:],
    }


def build_artifact_inspection_receipt_payload(
    *,
    command: str,
    receipt: dict[str, Any],
    registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build a compact receipt payload for artifact inspection commands."""
    summary = summarize_artifact_registry(registry)
    recent_artifacts = list(summary.get("recent_artifacts", []))
    return {
        "receipt_id": str(receipt.get("receipt_id") or ""),
        "action_id": str(receipt.get("action_id") or ""),
        "action_type": str(receipt.get("action_type") or ""),
        "reason_code": str(receipt.get("reason_code") or ""),
        "command": command,
        "command_classification": classify_artifact_command(command),
        "artifact_registry_summary": summary,
        "artifact_refs": recent_artifacts,
    }


def build_first_verified_success_record(
    *,
    artifact_registry: dict[str, dict[str, Any]],
    artifact_gate: dict[str, Any],
    verifier_status: dict[str, Any],
    receipt_id: str | None = None,
) -> dict[str, Any]:
    """Build a preserved snapshot for the first solver-visible verified success."""
    summary = summarize_artifact_registry(artifact_registry)

    def _dict_value(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    recent_artifacts = list(summary.get("recent_artifacts", []))
    return {
        "status": "locked",
        "receipt_id": str(receipt_id or ""),
        "artifact_gate": {
            "status": str(artifact_gate.get("status") or "unknown"),
            "reason_codes": _string_list(artifact_gate.get("reason_codes")),
            "required_paths": _string_list(artifact_gate.get("required_paths")),
            "missing_paths": _string_list(artifact_gate.get("missing_paths")),
            "empty_paths": _string_list(artifact_gate.get("empty_paths")),
            "observed_hashes": _dict_value(artifact_gate.get("observed_hashes")),
        },
        "verifier_status": {
            "status": str(verifier_status.get("status") or "unknown"),
            "reason_codes": _string_list(verifier_status.get("reason_codes")),
            "output_summary": str(verifier_status.get("output_summary") or ""),
        },
        "artifact_registry_summary": summary,
        "artifact_refs": [str(item.get("path") or "") for item in recent_artifacts if str(item.get("path") or "")],
    }


def check_required_artifacts(
    *,
    workspace_root: Path,
    required_paths: list[str],
) -> dict[str, Any]:
    """Check that required artifacts exist and are non-empty."""
    required = _dedupe_strings(_string_list(required_paths))
    required_artifacts: list[dict[str, Any]] = []
    normalized_required_paths: list[str] = []
    missing_paths: list[str] = []
    empty_paths: list[str] = []
    observed_hashes: dict[str, str] = {}
    for relpath in required:
        record = build_artifact_record(
            path=relpath,
            workspace_root=workspace_root,
            generated=False,
        )
        compact = _compact_artifact_record(record)
        required_artifacts.append(compact)
        path_key = str(record.get("path") or relpath or "")
        normalized_required_paths.append(path_key)
        if not bool(record.get("exists")):
            missing_paths.append(path_key)
            continue
        if not _is_non_empty_artifact(workspace_root, path_key):
            empty_paths.append(path_key)
            continue
        observed_hash = str(record.get("sha256") or "")
        if observed_hash:
            observed_hashes[path_key] = observed_hash
    reason_codes: list[str] = []
    if missing_paths:
        reason_codes.append("required_artifact_missing")
    if empty_paths:
        reason_codes.append("required_artifact_empty")
    status = "pass" if not reason_codes else "fail"
    return {
        "status": status,
        "reason_codes": reason_codes,
        "required_paths": normalized_required_paths,
        "required_artifacts": required_artifacts,
        "missing_paths": missing_paths,
        "empty_paths": empty_paths,
        "observed_hashes": observed_hashes,
        "hash_algorithm": "sha256",
        "required_count": len(normalized_required_paths),
        "present_count": len(normalized_required_paths) - len(missing_paths),
        "non_empty_count": len(normalized_required_paths) - len(missing_paths) - len(empty_paths),
    }


def _canonical_artifact_path(path: Path, workspace_root: Path) -> tuple[str, Path]:
    workspace_abs = workspace_root.resolve(strict=False)
    absolute_path = path if path.is_absolute() else workspace_abs / path
    absolute_path = absolute_path.resolve(strict=False)
    try:
        relative = absolute_path.relative_to(workspace_abs)
    except ValueError:
        return absolute_path.as_posix(), absolute_path
    if str(relative) == ".":
        return ".", absolute_path
    return relative.as_posix(), absolute_path


def _normalize_existing_registry(existing: dict[str, dict[str, Any]], workspace_root: Path) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    if not isinstance(existing, dict):
        return normalized
    for key, record in existing.items():
        if not isinstance(record, dict):
            continue
        path_value = record.get("path")
        if isinstance(path_value, str) and path_value:
            normalized_key = str(build_artifact_record(path=path_value, workspace_root=workspace_root).get("path") or "")
        elif isinstance(key, str) and key:
            normalized_key = str(build_artifact_record(path=key, workspace_root=workspace_root).get("path") or "")
        else:
            continue
        normalized[normalized_key] = dict(record)
        normalized[normalized_key]["path"] = normalized_key
    return normalized


def _compact_artifact_record(record: dict[str, Any]) -> dict[str, Any]:
    sha = str(record.get("sha256") or "")
    if len(sha) > 8:
        sha = sha[:7]
    return {
        "path": str(record.get("path") or ""),
        "exists": bool(record.get("exists")),
        "size_bytes": int(record.get("size_bytes") or 0),
        "sha256": sha,
        "suffix": str(record.get("suffix") or ""),
        "type_guess": str(record.get("type_guess") or "unknown"),
        "origin_receipt_id": record.get("origin_receipt_id"),
        "last_seen_receipt_id": record.get("last_seen_receipt_id"),
        "generated": bool(record.get("generated")),
        "freshness": str(record.get("freshness") or "unknown"),
    }


def _freshness_from_generated(*, exists: bool, generated: bool | None) -> str:
    if not exists:
        return "missing"
    if generated is True:
        return "generated"
    if generated is False:
        return "original"
    return "unknown"


def _clean_receipt_id(receipt_id: str | None) -> str | None:
    if not isinstance(receipt_id, str):
        return None
    receipt_id = receipt_id.strip()
    return receipt_id or None


def _is_non_empty_artifact(workspace_root: Path, relpath: str) -> bool:
    path_ref = _coerce_artifact_path_ref(relpath)
    if path_ref is None:
        return False
    _, absolute_path = _canonical_artifact_path(Path(path_ref), workspace_root)
    if not absolute_path.is_file():
        return False
    try:
        return absolute_path.stat().st_size > 0
    except OSError:
        return False
