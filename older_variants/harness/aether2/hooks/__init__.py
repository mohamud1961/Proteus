"""Aether-2 hook lifecycle surface."""

from older_variants.harness.aether2.hooks.builtins import deny_argument_substring, deny_tool_names, note_event
from older_variants.harness.aether2.hooks.lifecycle import (
    HookContext,
    HookEvent,
    HookInvocation,
    HookResult,
    HookRunResult,
    PermissionDecision,
    PermissionDecisionReason,
    deep_copy_arguments,
)
from older_variants.harness.aether2.hooks.registry import HookRegistry, RegisteredHook

__all__ = [
    "HookContext",
    "HookEvent",
    "HookInvocation",
    "HookRegistry",
    "HookResult",
    "HookRunResult",
    "PermissionDecision",
    "PermissionDecisionReason",
    "RegisteredHook",
    "deep_copy_arguments",
    "deny_argument_substring",
    "deny_tool_names",
    "note_event",
]
