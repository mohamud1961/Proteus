"""Automatic memory repeat-collision helpers.

The solver should not spend turns asking whether it has already read a file or
run a check.  These helpers infer a stable target from an action, find matching
ledger receipts, and produce a compact receipt that the next context packet can
surface as evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Mapping

from .ledger import ExecutionLedger, Receipt
from .memory_query import receipt_to_memory_result
from .runtime_ir import ActionRequest, EnvMap, normalize_relpath


_AUTOMATIC_KINDS = frozenset({
    "read_file", "write_file", "run_command", "run_check",
    "probe_service", "launch_process", "stop_process", "inspect_artifact",
    "read_output", "grep_output", "read_file_page",
})


@dataclass(frozen=True)
class ActionTarget:
    action_kind: str
    target_type: str
    key: str
    label: str
    explicit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _command_fingerprint(command: str) -> str:
    return re.sub(r"\s+", " ", str(command or "").strip())


def infer_action_target(action: ActionRequest, envmap: EnvMap) -> ActionTarget | None:
    explicit = action.target if isinstance(action.target, Mapping) else {}
    explicit_type = str(explicit.get("type", "")).strip()
    explicit_path = str(explicit.get("path", "")).strip()
    explicit_check = str(explicit.get("check_id", "")).strip()
    explicit_command = str(explicit.get("command_fingerprint", "")).strip()

    if explicit_path:
        path = normalize_relpath(explicit_path, envmap.workspace_root)
        return ActionTarget(action.kind, explicit_type or "file", path, f"{action.kind}:{path}", True)
    if explicit_check:
        return ActionTarget(action.kind, explicit_type or "check", explicit_check, f"{action.kind}:{explicit_check}", True)
    if explicit_command:
        command = _command_fingerprint(explicit_command)
        return ActionTarget(action.kind, explicit_type or "command", command, f"{action.kind}:{command}", True)

    if action.kind in {"read_file", "write_file", "read_file_page", "inspect_artifact"}:
        path = normalize_relpath(str(action.arguments.get("path", "")), envmap.workspace_root)
        if path:
            return ActionTarget(action.kind, "file", path, f"{action.kind}:{path}")
    if action.kind == "run_command":
        command = _command_fingerprint(str(action.arguments.get("command", "")))
        if command:
            return ActionTarget(action.kind, "command", command, f"run_command:{command}")
    if action.kind == "run_check":
        check_id = str(action.arguments.get("check_id", "")).strip()
        if check_id:
            return ActionTarget(action.kind, "check", check_id, f"run_check:{check_id}")
    if action.kind in {"probe_service", "stop_process"}:
        target = str(action.arguments.get("target", "")).strip()
        if target:
            return ActionTarget(action.kind, "service", target, f"{action.kind}:{target}")
    if action.kind == "launch_process":
        service = str(action.arguments.get("service_name", "")).strip()
        command = _command_fingerprint(str(action.arguments.get("command", "")))
        key = service or command
        if key:
            return ActionTarget(action.kind, "process", key, f"launch_process:{key}")
    if action.kind in {"read_output", "grep_output"}:
        handle = str(action.arguments.get("handle", "")).strip()
        pattern = str(action.arguments.get("pattern", "")).strip()
        if handle:
            key = f"{handle}|{pattern}" if action.kind == "grep_output" and pattern else handle
            return ActionTarget(action.kind, "output", key, f"{action.kind}:{key}")
    return None


def _receipt_matches(receipt: Receipt, action: ActionRequest, target: ActionTarget) -> bool:
    payload = receipt.payload or {}
    if action.kind == "read_file":
        return receipt.kind == "read_file" and str(payload.get("path", "")).strip() == target.key
    if action.kind == "write_file":
        paths = {str(path).strip() for path in payload.get("modified_paths", ()) or ()}
        paths.add(str(payload.get("path", "")).strip())
        return receipt.kind in {"read_file", "write_file", "run_command", "check_result"} and target.key in paths
    if action.kind == "run_command":
        return receipt.kind == "run_command" and _command_fingerprint(str(payload.get("command", ""))) == target.key
    if action.kind == "run_check":
        return receipt.kind == "check_result" and str(payload.get("check_id", "")).strip() == target.key
    if action.kind == "probe_service":
        return receipt.kind == "service_probe" and str(payload.get("target", "")).strip() == target.key
    if action.kind == "stop_process":
        return receipt.kind == "process_stop" and str(payload.get("target", "")).strip() == target.key
    if action.kind == "launch_process":
        return receipt.kind == "process_launch" and (
            str(payload.get("service_name", "")).strip() == target.key
            or _command_fingerprint(str(payload.get("command", ""))) == target.key
        )
    if action.kind == "inspect_artifact":
        return receipt.kind == "artifact_inspection" and str(payload.get("path", "")).strip() == target.key
    if action.kind == "read_file_page":
        return receipt.kind == "read_file_page" and str(payload.get("path", "")).strip() == target.key
    if action.kind in {"read_output", "grep_output"}:
        handle = str(payload.get("handle", "")).strip()
        pattern = str(payload.get("pattern", "")).strip()
        key = f"{handle}|{pattern}" if action.kind == "grep_output" and pattern else handle
        return receipt.kind == action.kind and key == target.key
    return False


def automatic_memory_receipt(
    action: ActionRequest,
    *,
    step: int,
    envmap: EnvMap,
    ledger: ExecutionLedger,
) -> Receipt | None:
    if action.kind not in _AUTOMATIC_KINDS:
        return None
    target = infer_action_target(action, envmap)
    if target is None:
        return None
    matches = [
        receipt for receipt in ledger.all_receipts()
        if _receipt_matches(receipt, action, target)
    ]
    if not matches:
        return None
    recent = matches[-4:]
    explicit_target = action.target if isinstance(action.target, Mapping) else {}
    justified = bool(
        str(action.arguments.get("repeat_justification", "")).strip()
        or str(action.arguments.get("why_repeat", "")).strip()
        or str(explicit_target.get("repeat_justification", "")).strip()
    )
    same_hashes = {
        str(receipt.payload.get("content_hash", "")).strip()
        for receipt in recent
        if receipt.payload.get("content_hash")
    }
    latest = recent[-1]
    guidance = (
        "Automatic memory found prior evidence for this target. Use the prior evidence, "
        "narrow the target, explain why a repeat is needed, or change strategy before "
        "spending more steps on the same action."
    )
    if justified:
        guidance = "Automatic memory found prior evidence; repeat was explicitly justified by the solver action."
    payload: dict[str, Any] = {
        "action_kind": action.kind,
        "target": target.as_dict(),
        "match_count": len(matches),
        "recent_evidence": [receipt_to_memory_result(receipt) for receipt in recent],
        "latest_receipt_id": latest.receipt_id,
        "same_content_hash": len(same_hashes) == 1 and bool(same_hashes),
        "repeat_justified": justified,
        "guidance": guidance,
    }
    return Receipt(
        receipt_id=f"step-{step}:{action.action_id}:automatic_memory",
        step=step,
        kind="automatic_memory",
        success=True,
        summary=f"automatic memory surfaced {len(matches)} prior event(s) for {target.label}",
        payload=payload,
    )
