"""Built-in hook helpers for the permission and tool-lifecycle hook surface."""

from __future__ import annotations

from typing import Iterable

from harness.aether2.hooks.lifecycle import HookContext, HookResult, PermissionDecision, PermissionDecisionReason


def deny_tool_names(tool_names: Iterable[str], *, message: str | None = None):
    """Create a PermissionRequest-style hook that denies a named tool set."""

    blocked = frozenset(str(name) for name in tool_names)

    def _callback(context: HookContext) -> HookResult | None:
        if context.tool_name not in blocked:
            return None
        deny_message = message or f"Permission denied by hook for tool {context.tool_name}"
        return HookResult(
            permission_decision=PermissionDecision(
                behavior="deny",
                message=deny_message,
                reason=PermissionDecisionReason(
                    type="hook",
                    source="builtin",
                    hook_name="deny_tool_names",
                    message=deny_message,
                ),
            ),
            note=f"denied {context.tool_name}",
        )

    return _callback


def deny_argument_substring(argument_key: str, substring: str, *, message: str):
    """Create a PermissionRequest-style hook that denies matching string arguments."""

    def _callback(context: HookContext) -> HookResult | None:
        value = context.arguments.get(argument_key)
        if not isinstance(value, str) or substring not in value:
            return None
        return HookResult(
            permission_decision=PermissionDecision(
                behavior="deny",
                message=message,
                reason=PermissionDecisionReason(
                    type="hook",
                    source="builtin",
                    hook_name="deny_argument_substring",
                    message=message,
                ),
            ),
            note=f"matched {argument_key}",
        )

    return _callback


def note_event(label: str):
    """Create a lightweight audit-only hook callback."""

    def _callback(_: HookContext) -> HookResult:
        return HookResult(note=label)

    return _callback


__all__ = ["deny_argument_substring", "deny_tool_names", "note_event"]
