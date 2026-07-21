"""Hook lifecycle primitives for the Aether harness."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal
import time

HookEvent = Literal["permission_request", "pre_tool_use", "post_tool_use"]
HookStatus = Literal["matched", "error"]
PermissionBehavior = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PermissionDecisionReason:
    """Structured reason metadata attached to a permission decision."""

    type: str
    source: str
    hook_name: str | None = None
    message: str | None = None
    rule_id: str | None = None


@dataclass(frozen=True)
class PermissionDecision:
    """Permission outcome representing the allow/deny/ask decision union."""

    behavior: PermissionBehavior
    message: str | None = None
    reason: PermissionDecisionReason | None = None
    updated_input: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "behavior": self.behavior,
            "message": self.message,
            "reason": None if self.reason is None else self.reason.__dict__.copy(),
            "updated_input": deepcopy(self.updated_input),
            "metadata": deepcopy(self.metadata),
        }


@dataclass(frozen=True)
class HookContext:
    """Visible hook invocation payload for one tool lifecycle event."""

    event: HookEvent
    tool_name: str
    arguments: dict[str, Any]
    call_id: str | None = None
    workspace_root: str | None = None
    permission_decision: PermissionDecision | None = None
    observation: Any = None
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    @classmethod
    def create(
        cls,
        *,
        event: HookEvent,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str | None = None,
        workspace_root: str | None = None,
        permission_decision: PermissionDecision | None = None,
        observation: Any = None,
    ) -> "HookContext":
        return cls(
            event=event,
            tool_name=tool_name,
            arguments=deepcopy(arguments),
            call_id=call_id,
            workspace_root=workspace_root,
            permission_decision=permission_decision,
            observation=observation,
        )


@dataclass(frozen=True)
class HookResult:
    """Hook result: an audit note plus an optional permission decision."""

    permission_decision: PermissionDecision | None = None
    note: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookInvocation:
    """Audited record of one hook callback attempt."""

    hook_name: str
    event: HookEvent
    matcher: str
    status: HookStatus
    duration_sec: float
    note: str | None = None
    decision: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hook_name": self.hook_name,
            "event": self.event,
            "matcher": self.matcher,
            "status": self.status,
            "duration_sec": round(self.duration_sec, 6),
            "note": self.note,
            "decision": deepcopy(self.decision),
            "error": self.error,
        }


@dataclass(frozen=True)
class HookRunResult:
    """Aggregated hook execution results for one lifecycle event."""

    invocations: list[HookInvocation] = field(default_factory=list)
    permission_decision: PermissionDecision | None = None


def deep_copy_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy so hook and tool inputs stay immutable and auditable."""

    return deepcopy(arguments)


__all__ = [
    "HookContext",
    "HookEvent",
    "HookInvocation",
    "HookResult",
    "HookRunResult",
    "PermissionBehavior",
    "PermissionDecision",
    "PermissionDecisionReason",
    "deep_copy_arguments",
]
