"""Context compiler: builds the per-step context packet for the solver.

Extracted from ledger.py to stay under the 500-LOC cap.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import context_views as _views
from .context_recipe_apply import apply_recipe as _apply_recipe_fn
from .memory_events import artifact_history, memory_events
from .runtime_ir import stable_json

if TYPE_CHECKING:
    from .ledger import ExecutionLedger, Receipt
    from .monitors import MonitorAlert
    from .runtime_ir import CompiledRuntime, ContextPolicy, ContextRecipe


_STANDARD_SECTION_KEYS = _views._STANDARD_SECTION_KEYS

_SUPPORTED_EXACT_RECIPE_SELECTORS = _views._SUPPORTED_EXACT_RECIPE_SELECTORS

_RECENT_RECIPE_SELECTORS = _views._RECENT_RECIPE_SELECTORS

_TOOL_RESULT_KINDS = _views._TOOL_RESULT_KINDS


class ContextCompiler:
    # Receipt/section view builders live in context_views (500-LOC cap);
    # bound here so existing self._x call sites keep working.
    _recent_receipts = staticmethod(_views.recent_receipts)
    _last_failures = staticmethod(_views.last_failures)
    _latest_tool_receipt = staticmethod(_views.latest_tool_receipt)
    _queryable_section_meta = staticmethod(_views.queryable_section_meta)
    _queryable_receipt_meta = staticmethod(_views.queryable_receipt_meta)
    _receipt_inline_view = staticmethod(_views.receipt_inline_view)
    _latest_file_reads = staticmethod(_views.latest_file_reads)
    _memory_loop_feedback = staticmethod(_views.memory_loop_feedback)
    _automatic_memory_findings = staticmethod(_views.automatic_memory_findings)
    _action_constraints_from_no_progress = staticmethod(_views.action_constraints_from_no_progress)
    _maybe_compress = staticmethod(_views.maybe_compress)

    def compile(
        self,
        compiled: CompiledRuntime,
        ledger: ExecutionLedger,
        alerts: list["MonitorAlert"],
    ) -> dict[str, Any]:
        policy = compiled.context_policy
        available = self._available_sections(compiled, ledger, alerts)
        if policy.recipe is None:
            packet = self._apply_mode(
                policy.mode,
                self._select_standard_sections(policy, available, always_include_pending=bool(compiled.planned_checks())),
                ledger,
            )
        else:
            packet = _apply_recipe_fn(compiled, ledger, policy.recipe, available, policy.mode)
        packet = self._enforce_safety_sections(packet, available)
        packet = self._maybe_compress(packet, policy)
        if "context_recipe_realization" in packet:
            packet = self._finalize_recipe_realization(packet)
        return packet

    def _enforce_safety_sections(self, packet: dict[str, Any], available: dict[str, Any]) -> dict[str, Any]:
        out = dict(packet)
        out.setdefault("automatic_memory_available", True)
        for key in (
            "active_completion_findings",
            "pending_checks",
            "automatic_memory_findings",
            "no_progress_controls",
            "action_constraints",
            "solver_parse_errors",
            "blocked_denied_receipts",
            "output_handles",
        ):
            if key in available and key not in out:
                out[key] = available[key]
        # The solver must always see the output of tools it just used, not only
        # shell commands.  Otherwise typed tools such as service probes, artifact
        # inspection, process launch, read_output, and read_file write useful
        # evidence into the ledger but vanish from the next model-visible packet.
        # Respect an explicit recipe decision to make a result section queryable
        # rather than inline; otherwise force the evidence into the packet.
        realization = packet.get("context_recipe_realization")
        queryable_selectors: set[str] = set()
        if isinstance(realization, dict):
            queryable_selectors = {
                str(item.get("selector", ""))
                for item in realization.get("queryable_not_inline", []) or []
                if isinstance(item, dict)
            }
        # A recipe may externalize a dedicated section (command_results,
        # file_reads, ...) as queryable-not-inline for token budget.  When it
        # does, the corresponding receipt kind must also be dropped from the
        # forced tool_results copy so the large payload the recipe chose to
        # externalize cannot leak back in through this channel.
        _queryable_kind_of = {"file_reads": "read_file", "file_writes": "write_file"}
        suppressed_kinds = {
            kind for selector, kind in _queryable_kind_of.items() if selector in queryable_selectors
        }
        if "tool_results" in available and "tool_results" not in out and "tool_results" not in queryable_selectors:
            rows = available["tool_results"]
            if suppressed_kinds:
                rows = [row for row in rows if row.get("kind") not in suppressed_kinds]
            out["tool_results"] = rows
        if "command_results" in available and "command_results" not in out and "command_results" not in queryable_selectors:
            out["command_results"] = available["command_results"]
        return out

    def _available_sections(
        self,
        compiled: CompiledRuntime,
        ledger: ExecutionLedger,
        alerts: list["MonitorAlert"],
    ) -> dict[str, Any]:
        packet: dict[str, Any] = {
            "open_obligations": [item.as_dict() for item in ledger.open_obligations()],
            "obligation_status": ledger.obligation_snapshot(),
            "monitor_alerts": [
                {
                    "code": alert.code,
                    "message": alert.message,
                    "severity": alert.severity,
                    "blocker_code": alert.blocker_code,
                    "recommend_reconfigure": alert.recommend_reconfigure,
                }
                for alert in alerts[: compiled.context_policy.max_alerts]
            ],
            "live_processes": ledger.live_processes(),
            "recent_progress": [
                {
                    "receipt_id": receipt.receipt_id,
                    "kind": receipt.kind,
                    "summary": receipt.summary,
                }
                for receipt in ledger.recent_progress(compiled.context_policy.max_recent_receipts)
            ],
            "failure_clusters": ledger.failure_clusters(compiled.context_policy.max_failure_clusters),
            "artifacts_present": sorted(ledger.current_artifacts()),
            "candidate_leaderboard": ledger.candidate_leaderboard(compiled.context_policy.max_candidates),
            "installed_capabilities": sorted(ledger.installed_capabilities),
            "planned_checks": [
                {
                    "check_id": check.check_id,
                    "label": check.label,
                    "command": check.command,
                    "origin": check.origin,
                }
                for check in compiled.planned_checks()
            ],
        }

        planned = compiled.planned_checks()
        latest = {outcome.check_id: outcome for outcome in ledger.latest_checks(compiled.check_plan_ids)}
        pending: list[dict[str, str | bool | None]] = []
        for check in planned[:16]:
            outcome = latest.get(check.check_id)
            passed: bool | None = outcome.passed if outcome is not None else None
            failure_kind = ""
            detail = ""
            if outcome is not None and not outcome.passed:
                failure_kind = outcome.blocker_code or "check_failed"
                detail = outcome.detail
            pending.append({
                "label": check.label,
                "command_short": check.command[:80],
                "passed": passed,
                "failure_kind": failure_kind,
                "repair_hint": _repair_hint(check.label, failure_kind, detail),
            })
        if pending:
            packet["pending_checks"] = pending

        repeated = ledger.repeated_actions()
        if repeated:
            packet["repeated_actions"] = repeated
            packet["repeat_efficiency_guidance"] = {
                "principle": "Repeated actions are an information-gain signal, not a correctness verdict.",
                "rule": (
                    "Do not repeat the same command, read, write, or submit claim unless the repeat adds new information, "
                    "changes task state, or creates missing inspectable evidence. Use prior output handles/evidence when enough."
                ),
                "next_step_question": "What new information, state change, or evidence would this repeat add?",
            }

        already_read = ledger.files_already_read()
        if already_read:
            packet["files_already_read"] = already_read

        latest_reads = self._latest_file_reads(ledger, limit=3)
        if latest_reads:
            packet["latest_file_reads"] = latest_reads

        max_recent = max(0, compiled.context_policy.max_recent_receipts)
        # tool_results carries the typed-tool evidence that has no dedicated
        # inline section of its own (service probes, artifact inspection,
        # read_output, process launch/stop, ...).  run_command keeps its
        # canonical home in command_results below; rendering it here too would
        # double the largest payload in the packet and bypass a recipe that made
        # command_results queryable-not-inline for token budget.
        tool_results = [
            self._receipt_inline_view(receipt)
            for receipt in ledger.all_receipts()
            if receipt.kind in _TOOL_RESULT_KINDS and receipt.kind != "run_command"
        ][-max_recent:]
        packet["tool_results"] = tool_results

        command_results = [
            self._receipt_inline_view(receipt)
            for receipt in ledger.all_receipts()
            if receipt.kind == "run_command"
        ][-max_recent:]
        packet["command_results"] = command_results

        memory_loop = self._memory_loop_feedback(ledger)
        if memory_loop:
            packet["memory_loop_feedback"] = memory_loop

        automatic_memory = self._automatic_memory_findings(ledger)
        if automatic_memory:
            packet["automatic_memory_findings"] = automatic_memory

        no_progress_controls = [
            self._receipt_inline_view(receipt)
            for receipt in ledger.all_receipts()
            if receipt.kind == "no_progress_control"
        ][-4:]
        if no_progress_controls:
            packet["no_progress_controls"] = no_progress_controls
            packet["action_constraints"] = self._action_constraints_from_no_progress(no_progress_controls)
        parse_errors = [
            self._receipt_inline_view(receipt)
            for receipt in ledger.all_receipts()
            if receipt.kind == "solver_parse_error"
        ][-4:]
        if parse_errors:
            packet["solver_parse_errors"] = parse_errors

        denied = [
            self._receipt_inline_view(receipt)
            for receipt in ledger.all_receipts()
            if receipt.kind in {"unsupported_solver_reconfigure", "action_validation", "safety_block", "unknown_action", "automatic_memory_block"}
        ][-8:]
        if denied:
            packet["blocked_denied_receipts"] = denied

        handles: list[dict[str, Any]] = []
        for receipt in ledger.all_receipts():
            payload = receipt.payload or {}
            if payload.get("stdout_handle"):
                handles.append({"handle": payload.get("stdout_handle"), "receipt_id": receipt.receipt_id, "stream": "stdout", "bytes": payload.get("stdout_bytes", 0)})
            if payload.get("stderr_handle"):
                handles.append({"handle": payload.get("stderr_handle"), "receipt_id": receipt.receipt_id, "stream": "stderr", "bytes": payload.get("stderr_bytes", 0)})
            if payload.get("file_handle"):
                handles.append({"handle": payload.get("file_handle"), "receipt_id": receipt.receipt_id, "stream": "file", "bytes": payload.get("bytes", 0), "path": payload.get("path", "")})
        if handles:
            packet["output_handles"] = handles[-16:]

        no_progress_streak = ledger.no_progress_streak()
        if no_progress_streak:
            packet["stuck"] = {
                "no_progress": no_progress_streak >= 3,
                "no_progress_streak": no_progress_streak,
            }

        active_findings = ledger.active_finding_context(len(ledger.all_receipts()))
        if active_findings:
            packet["active_completion_findings"] = active_findings
        artifact_rows = artifact_history(ledger.all_receipts(), limit=12)
        if artifact_rows:
            packet["artifact_history"] = artifact_rows
        event_rows = memory_events(ledger.all_receipts(), limit=20)
        if event_rows:
            packet["memory_events"] = event_rows
        latest_failure = self._last_failures(ledger, 1)
        if latest_failure:
            packet["latest_failure"] = self._receipt_inline_view(latest_failure[-1])
        failed_checks = [
            self._receipt_inline_view(receipt)
            for receipt in ledger.all_receipts()
            if receipt.kind == "check_result" and not receipt.success
        ][-8:]
        if failed_checks:
            packet["failed_checks"] = failed_checks
        observations = [
            self._receipt_inline_view(receipt)
            for receipt in ledger.all_receipts()
            if receipt.kind == "record_observation" and receipt.success
        ][-8:]
        if observations:
            packet["observations"] = observations
        return packet

    def _select_standard_sections(
        self,
        policy: ContextPolicy,
        available: dict[str, Any],
        *,
        always_include_pending: bool,
    ) -> dict[str, Any]:
        packet: dict[str, Any] = {}
        for key in _STANDARD_SECTION_KEYS:
            if key in policy.include_sections:
                packet[key] = available[key]
        if "pending_checks" in available and (always_include_pending or "pending_checks" in policy.include_sections):
            packet["pending_checks"] = available["pending_checks"]
        for key in ("repeated_actions", "repeat_efficiency_guidance", "files_already_read", "latest_file_reads", "memory_loop_feedback", "automatic_memory_findings", "no_progress_controls", "action_constraints", "stuck", "active_completion_findings"):
            if key in available:
                packet[key] = available[key]
        return packet

    def _apply_mode(self, mode: str, packet: dict[str, Any], ledger: ExecutionLedger) -> dict[str, Any]:
        if mode == "default_bounded":
            return packet
        if mode == "retrieval_augmented":
            enriched = dict(packet)
            enriched["automatic_memory_available"] = True
            enriched["automatic_memory_guidance"] = (
                "Memory repeat interception is automatic. When prior evidence matches a proposed read, command, check, or overwrite, "
                "use that surfaced evidence, narrow the target, justify the repeat, or change strategy."
            )
            return enriched
        if mode == "latest_tool_result_only":
            latest = self._latest_tool_receipt(ledger)
            result = {"automatic_memory_available": True}
            if latest:
                result["latest_tool_result"] = latest
            for key in ("pending_checks", "active_completion_findings", "no_progress_controls", "action_constraints", "stuck"):
                if key in packet:
                    result[key] = packet[key]
            return result
        if mode == "rolling_recent":
            return {
                "recent_progress": packet.get("recent_progress", []),
                "pending_checks": packet.get("pending_checks", []),
                "artifacts_present": packet.get("artifacts_present", []),
                "active_completion_findings": packet.get("active_completion_findings", []),
                "automatic_memory_available": True,
            }
        if mode == "failure_focused":
            keys = (
                "active_completion_findings",
                "pending_checks",
                "failure_clusters",
                "repeated_actions",
                "repeat_efficiency_guidance",
                "files_already_read",
                "no_progress_controls",
                "action_constraints",
                "stuck",
            )
            return {key: packet[key] for key in keys if key in packet} | {"automatic_memory_available": True}
        return packet

    def _finalize_recipe_realization(self, packet: dict[str, Any]) -> dict[str, Any]:
        body = {key: value for key, value in packet.items() if key != "context_recipe_realization"}
        realization = dict(packet["context_recipe_realization"])
        counts = dict(realization.get("counts", {}))
        counts["rendered_sections"] = len(body)
        counts["nonempty_sections"] = sum(1 for value in body.values() if _item_count(value) > 0)
        realization["counts"] = counts
        realization["rendered_section_counts"] = {
            key: _item_count(value)
            for key, value in sorted(body.items())
        }
        byte_count = len(stable_json(body).encode("utf-8"))
        realization["byte_count_v1"] = byte_count
        realization["token_estimate_v1"] = max(1, (byte_count + 3) // 4)
        finalized = dict(packet)
        finalized["context_recipe_realization"] = realization
        return finalized


_item_count = _views.item_count


def _repair_hint(label: str, failure_kind: str, detail: str) -> str:
    if not failure_kind:
        return ""
    if failure_kind == "check_broken":
        return "Do not retry this check command; treat it as invalid check evidence and continue fixing the task artifact."
    if label.startswith("exists:"):
        target = label.split(":", 1)[1]
        return f"Create or write the required artifact at {target}; do not just rerun the existence check."
    if label.startswith("schema:"):
        target = label.split(":", 1)[1]
        if target.lower().endswith(".csv"):
            return f"Update {target} so its CSV header contains the required columns."
        return f"Update {target} so the structured output contains the required keys."
    if label.startswith("size:"):
        target = label.split(":", 1)[1]
        return f"Adjust {target} to satisfy the file-size threshold."
    if detail:
        return (
            "Use the failure detail to make the underlying task condition true; "
            "do not edit task tests or check files just to satisfy the checker. "
            "If the check command itself is mis-invoked or environment-broken, report the blocker."
        )
    return "Change the task artifact or strategy before rechecking; do not patch tests/checks as a shortcut."
