"""Small deterministic bridge for the canonical Aether-Next runtime.

This module deliberately stops at the trusted state/context boundary.  It is
not a second harness and does not execute tools, services, verifier routes, or
provider workers.  The production kernel can use it as the lossless bridge
between immutable task truth, dynamic world state, and model context while
those higher-level integrations are wired separately.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from .context_epochs import (
    CacheManifest,
    ContextEpoch,
    ContextManager,
    build_checkpoint,
    build_stable_prefix,
)
from .receipts import ReceiptStore
from .task_contract import TaskContract
from .world import StableEnvMap, WorldState


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


@dataclass(frozen=True)
class RuntimeCompilation:
    """Digest-only compilation metadata exposed by this bridge.

    The raw Architect configuration is intentionally not copied into solver
    context.  Consumers can compare the digest/version without leaking a
    verifier prompt, traps, or inspection strategy.
    """

    config_version: int
    stable_config_sha256: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "stable_config_sha256": self.stable_config_sha256,
        }


@dataclass(frozen=True)
class RuntimeSelection:
    """Realised context selector used by the canonical deterministic bridge.

    This is intentionally a small runtime value object rather than a second
    config namespace: the raw selector contract remains part of the typed
    context policy, while the value is resolved from current WorldState.
    """

    selector_index: int
    kind: str
    target: str | None
    representation: str
    inline_value: Any
    retrieval_handle: str | None
    raw_chars: int
    rendered_chars: int
    truncated: bool


KERNEL_CONSTITUTION = (
    "Aether-Next trusted kernel. Preserve immutable task truth, expose the "
    "fixed generic action surface, retain exact receipts, and keep stable "
    "environment facts separate from dynamic world state."
)
RESPONSE_PROTOCOL = {
    "solver_turn": ["act", "submit_outcome", "report_blocker"],
    "action_results": "typed receipts with lossless retrieval handles",
    "environment_model": "versioned stable EnvMap plus append-only dynamic-state deltas",
}
FIXED_TOOL_SCHEMA = {
    "tools": (
        "run_command",
        "read_file",
        "write_file",
        "launch_process",
        "probe_service",
        "stop_process",
        "inspect_artifact",
        "read_output",
        "grep_output",
    )
}


class HarnessRuntime:
    """Trusted task/world/context bridge for canonical Aether-Next.

    This class intentionally does not implement verifier routing, process
    lifecycle, or provider execution.  Its scope is deterministic state and
    context behaviour that can be tested without a model or VM.
    """

    def __init__(
        self,
        *,
        contract: TaskContract,
        envmap: Mapping[str, Any] | StableEnvMap,
        world: WorldState,
        raw_config: Mapping[str, Any] | None = None,
        config_version: int = 1,
        max_events: int = 64,
        max_dynamic_bytes: int = 64_000,
        max_inline_chars: int = 2_048,
    ) -> None:
        if not isinstance(contract, TaskContract):
            raise TypeError("contract must be a TaskContract")
        if world.task_contract != contract:
            raise ValueError("world task contract must match runtime task contract")
        if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version <= 0:
            raise ValueError("config_version must be a positive integer")
        self.contract = contract
        self.envmap = envmap if isinstance(envmap, StableEnvMap) else StableEnvMap.create(envmap)
        self.world = world
        # The runtime-owned EnvMap is the stable authority.  Dynamic facts in
        # ``world.env_facts`` remain separate and are never used to overwrite
        # this prefix value during ordinary action progress.
        self.world.stable_envmap = self.envmap
        self.receipts: ReceiptStore = world.receipts
        self._max_events = max_events
        self._max_dynamic_bytes = max_dynamic_bytes
        self._max_inline_chars = max_inline_chars
        self._raw_config = deepcopy(dict(raw_config or {}))
        context_raw = self._raw_config.get("context_policy", {})
        if "context_policy" in self._raw_config and not isinstance(context_raw, Mapping):
            raise ValueError("context_policy must be a mapping")
        if isinstance(context_raw, Mapping):
            self._max_events = self._positive_int(context_raw.get("max_events_before_compaction", self._max_events), "max_events_before_compaction")
            self._max_dynamic_bytes = self._positive_int(context_raw.get("max_dynamic_bytes", self._max_dynamic_bytes), "max_dynamic_bytes")
        self._last_selections: tuple[RuntimeSelection, ...] = ()
        self.compiled = self._compile(self._raw_config, config_version)
        self._context_epoch_id = 1
        self._install_context()
        self.receipts.append("config_realisation", self.config_realisation_payload())
        self.receipts.append("stable_envmap_installed", self.envmap.to_payload())

    @staticmethod
    def _compile(raw_config: Mapping[str, Any], config_version: int) -> RuntimeCompilation:
        digest = sha256(_canonical(raw_config).encode("utf-8")).hexdigest()
        return RuntimeCompilation(config_version=config_version, stable_config_sha256=digest)

    def _install_context(self) -> None:
        selections = self._realise_selectors()
        self._last_selections = selections
        prefix = build_stable_prefix(
            kernel_constitution=KERNEL_CONSTITUTION,
            fixed_tool_schema=FIXED_TOOL_SCHEMA,
            task_contract=self.contract,
            envmap=self.envmap,
            compiled_workbench=self.compiled.to_payload(),
            architect_solver_prompt="",
            response_protocol=RESPONSE_PROTOCOL,
        )
        checkpoint = build_checkpoint(
            selections=selections,
            active_findings=self.world.active_findings,
            latest_result=self.world.latest_result,
            dynamic_world_state=self.world.dynamic_snapshot(),
        )
        self.context = ContextManager(
            prefix,
            ContextEpoch(self.compiled.config_version, self._context_epoch_id, checkpoint),
            max_events=self._max_events,
            max_dynamic_bytes=self._max_dynamic_bytes,
            receipts=self.receipts,
            max_inline_chars=self._max_inline_chars,
        )

    def config_realisation_payload(self) -> dict[str, Any]:
        return {
            **self.compiled.to_payload(),
            "stable_envmap_version": self.envmap.version,
            "stable_envmap_sha256": self.envmap.sha256,
            "fixed_tools": list(FIXED_TOOL_SCHEMA["tools"]),
            "selector_realisation": [
                {
                    "selector_index": item.selector_index,
                    "kind": item.kind,
                    "target": item.target,
                    "representation": item.representation,
                    "resolved": item.inline_value is not None,
                    "truncated": item.truncated,
                    "retrieval_handle": item.retrieval_handle,
                }
                for item in self._last_selections
            ],
        }

    @staticmethod
    def _head_tail(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        head = max(1, limit // 2)
        tail = max(1, limit - head)
        return value[:head] + "\n...[middle omitted; use handle]...\n" + value[-tail:]

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, str):
            return value
        return _canonical(value)

    def _lookup_selector(self, kind: str, target: str | None) -> Any:
        if kind == "task_contract":
            return {
                "raw_task_prompt": self.contract.raw_task_prompt,
                "clauses": [clause.__dict__ for clause in self.contract.clauses],
            }
        if kind == "env_fact":
            return self.world.env_facts.get(target or "")
        if kind == "file":
            return self.world.files.get(target or "")
        if kind == "receipt":
            try:
                return self._solver_safe_receipt_payload(self.receipts.get(target or "").payload)
            except KeyError:
                return None
        if kind == "artifact":
            return self.world.artifacts.get(target or "")
        if kind == "service_state":
            return self.world.services.get(target or "")
        if kind == "job_state":
            return self.world.jobs.get(target or "")
        if kind == "active_findings":
            return list(self.world.active_findings)
        if kind == "latest_result":
            return self.world.latest_result
        if kind == "named_section":
            return self.world.named_sections.get(target or "")
        raise ValueError(f"unsupported context selector kind: {kind}")

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be a positive integer") from None
        if parsed <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return parsed

    @classmethod
    def _solver_safe_receipt_payload(cls, value: Any) -> Any:
        """Remove role-owned strategy/journey material from receipt views."""
        forbidden_fragments = (
            "prompt", "journey", "strategy", "trap", "solver", "verifier",
            "proof", "observation", "blocker",
        )
        if isinstance(value, Mapping):
            safe: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key).lower()
                if any(fragment in key_text for fragment in forbidden_fragments):
                    continue
                safe[str(key)] = cls._solver_safe_receipt_payload(item)
            return safe
        if isinstance(value, list):
            return [cls._solver_safe_receipt_payload(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._solver_safe_receipt_payload(item) for item in value)
        return deepcopy(value)

    def _realise_selectors(self) -> tuple[RuntimeSelection, ...]:
        context_raw = self._raw_config.get("context_policy", {})
        rows = context_raw.get("selectors", ()) if isinstance(context_raw, Mapping) else ()
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("context_policy.selectors must be a sequence")
        realised: list[RuntimeSelection] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError(f"context selector {index} must be a mapping")
            kind = str(row.get("kind", "")).strip()
            target = row.get("target")
            target = str(target) if target is not None else None
            representation = str(row.get("representation", "structured_summary"))
            required = bool(row.get("required", False))
            max_chars = int(row.get("max_chars", 4000))
            if max_chars <= 0:
                raise ValueError("selector max_chars must be positive")
            value = self._lookup_selector(kind, target)
            if value is None:
                if required:
                    raise ValueError(f"required selector unresolved before Solver start: {kind}:{target}")
                realised.append(RuntimeSelection(index, kind, target, representation, None, None, 0, 0, False))
                continue
            raw_text = self._stringify(value)
            truncated = False
            if representation == "full":
                rendered: Any = raw_text
            elif representation == "head_tail":
                rendered = self._head_tail(raw_text, max_chars)
                truncated = len(raw_text) > max_chars
            elif representation == "targeted_excerpt":
                pattern = str(row.get("pattern", ""))
                if not pattern:
                    raise ValueError("targeted_excerpt requires pattern")
                match = re.search(pattern, raw_text, flags=re.IGNORECASE | re.MULTILINE)
                rendered = self._head_tail(raw_text, max_chars) if not match else raw_text[max(0, match.start() - max_chars // 2):match.end() + max_chars // 2]
                truncated = rendered != raw_text
            elif representation == "structured_summary":
                if isinstance(value, Mapping):
                    rendered = {"type": "mapping", "keys": sorted(str(key) for key in value), "preview": {str(key): value[key] for key in list(value)[:8]}}
                elif isinstance(value, list):
                    rendered = {"type": "list", "count": len(value), "preview": value[:8]}
                else:
                    rendered = {"type": type(value).__name__, "chars": len(raw_text), "preview": raw_text[:500]}
                truncated = len(raw_text) > len(self._stringify(rendered))
            elif representation == "handle_only":
                rendered = {"available": True, "chars": len(raw_text)}
                truncated = True
            else:
                raise ValueError(f"unsupported selector representation: {representation}")
            handle = None
            # A selector asking for a full view still cannot force a large
            # payload into the model request.  Preserve the exact value behind
            # the same receipt-backed handle contract used by truncated views.
            if not truncated and len(raw_text) > self._max_inline_chars:
                rendered = {"available": True, "chars": len(raw_text)}
                truncated = True
            if truncated or representation == "handle_only":
                receipt = self.receipts.append_deduplicated(
                    "context_retrieval_payload",
                    {"selector_kind": kind, "target": target, "value": value},
                )
                handle = receipt.receipt_id
                if isinstance(rendered, Mapping):
                    rendered = {**rendered, "retrieval_handle": handle}
                else:
                    rendered = {"excerpt": rendered, "retrieval_handle": handle}
            rendered_text = self._stringify(rendered)
            realised.append(RuntimeSelection(index, kind, target, representation, rendered, handle, len(raw_text), len(rendered_text), truncated))
        return tuple(realised)

    def request(self, usage: Mapping[str, Any] | None = None) -> tuple[bytes, CacheManifest]:
        """Render solver context and a local/provider cache evidence manifest."""

        return self.context.current_request(usage)

    def _refresh_checkpoint_and_compact(self) -> None:
        self._last_selections = self._realise_selectors()
        checkpoint = build_checkpoint(
            selections=self._last_selections,
            active_findings=self.world.active_findings,
            latest_result=self.world.latest_result,
            dynamic_world_state=self.world.dynamic_snapshot(),
        )
        if self.context.compact_if_needed(checkpoint):
            self.receipts.append(
                "context_compaction",
                {
                    "config_version": self.compiled.config_version,
                    "stable_envmap_version": self.envmap.version,
                    "new_epoch_id": self.context.epoch.epoch_id,
                    "preserved_dynamic_state_version": self.world.state_version,
                },
            )

    def record_action_result(
        self,
        *,
        action_id: str,
        action_kind: str,
        result: Mapping[str, Any],
        state_delta: Mapping[str, Any] | None = None,
        step: int | None = None,
    ) -> str:
        """Persist an exact action receipt and atomically apply its state delta."""

        if not isinstance(result, Mapping):
            raise TypeError("result must be a mapping")
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("action_id must be non-empty")
        if not isinstance(action_kind, str) or not action_kind.strip():
            raise ValueError("action_kind must be non-empty")
        typed_delta = dict(state_delta or {})
        # WorldState validates and commits through a copy.  A malformed delta
        # therefore leaves both world state and receipts untouched.
        messages = self.world.apply_delta(typed_delta, step=step)
        receipt = self.receipts.append(
            "action_result",
            {
                "action_id": action_id,
                "action_kind": action_kind,
                # Receipts retain the exact payload; only the checkpoint and
                # rendered context use the compact representation below.
                "result": deepcopy(dict(result)),
                "state_delta": deepcopy(typed_delta),
                "dynamic_state_messages": list(messages),
                "dynamic_state_version": self.world.state_version,
            },
        )
        # latest_result is a compact checkpoint; the exact payload remains in
        # the receipt and can be retrieved by receipt id.
        result_json = _canonical(result)
        if len(result_json) > self._max_inline_chars:
            handle = self.context.output_handles.put(result_json, kind="context_event_output")
            compact_result: Mapping[str, Any] = {
                "type": "large_action_result",
                "chars": len(result_json),
                "keys": sorted(str(key) for key in result),
                "output_handle": handle,
                "sha256": sha256(result_json.encode("utf-8")).hexdigest(),
            }
        else:
            compact_result = deepcopy(dict(result))
        self.world.latest_result = {
            "receipt_id": receipt.receipt_id,
            "action_id": action_id,
            "action_kind": action_kind,
            "result": compact_result,
        }
        self.context.append_event(
            {
                "kind": "action_result",
                "receipt_id": receipt.receipt_id,
                "action_id": action_id,
                "action_kind": action_kind,
                "state_delta": deepcopy(typed_delta),
                "dynamic_state_messages": list(messages),
                "dynamic_state_version": self.world.state_version,
                "result": deepcopy(dict(compact_result)),
                "active_findings": deepcopy(self.world.active_findings),
            }
        )
        self._refresh_checkpoint_and_compact()
        return receipt.receipt_id

    def revise_envmap(
        self,
        *,
        changes: Mapping[str, Any],
        reason: str,
        evidence_receipt_ids: Sequence[str],
    ) -> None:
        """Revise stable environment facts only with receipt-backed evidence."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("EnvMap revision requires a non-empty reason")
        if not evidence_receipt_ids:
            raise ValueError("EnvMap revision requires at least one evidence receipt")
        for receipt_id in evidence_receipt_ids:
            self.receipts.get(receipt_id)
        previous = self.envmap
        revised = previous.revise(
            changes=changes,
            reason=reason,
            evidence_receipt_ids=evidence_receipt_ids,
        )
        previous_prefix = self.context.prefix.sha256
        self.envmap = revised
        # A stable-prefix change starts a fresh epoch namespace.  Epoch IDs
        # count dynamic compactions within that prefix; they do not pretend
        # that events from the old cacheable prefix remain append-only.
        self._context_epoch_id = 1
        self._install_context()
        self.receipts.append(
            "stable_envmap_revised",
            {
                "previous_version": previous.version,
                "new_version": revised.version,
                "previous_envmap_sha256": previous.sha256,
                "new_envmap_sha256": revised.sha256,
                "previous_stable_prefix_sha256": previous_prefix,
                "new_stable_prefix_sha256": self.context.prefix.sha256,
                "changes": deepcopy(dict(changes)),
                "reason": reason,
                "evidence_receipt_ids": list(evidence_receipt_ids),
                "new_context_epoch_id": self.context.epoch.epoch_id,
            },
        )

    def reconfigure(
        self,
        raw_config: Mapping[str, Any] | None = None,
        *,
        config_version: int | None = None,
        force: bool = False,
    ) -> RuntimeCompilation:
        """Update digest/version metadata and rebuild the stable context prefix."""

        # ``force`` is accepted for the migrated deterministic API.  The
        # production kernel still gates reconfiguration on an owner-verified
        # blocker; this model-free bridge has no pending blocker to bypass.
        del force
        next_version = self.compiled.config_version + 1 if config_version is None else config_version
        if isinstance(next_version, bool) or not isinstance(next_version, int) or next_version <= self.compiled.config_version:
            raise ValueError("reconfiguration version must increase")
        next_config = deepcopy(dict(self._raw_config if raw_config is None else raw_config))
        self._raw_config = next_config
        context_raw = next_config.get("context_policy", {})
        if "context_policy" in next_config and not isinstance(context_raw, Mapping):
            raise ValueError("context_policy must be a mapping")
        if isinstance(context_raw, Mapping):
            self._max_events = self._positive_int(context_raw.get("max_events_before_compaction", self._max_events), "max_events_before_compaction")
            self._max_dynamic_bytes = self._positive_int(context_raw.get("max_dynamic_bytes", self._max_dynamic_bytes), "max_dynamic_bytes")
        self.compiled = self._compile(next_config, next_version)
        self._context_epoch_id = 1
        self._install_context()
        self.receipts.append("config_reconfigured", self.config_realisation_payload())
        return self.compiled
