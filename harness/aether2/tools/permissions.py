"""Owned permission substrate for the Aether harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
import json

from harness.aether2.hooks.lifecycle import HookContext, HookInvocation, PermissionDecision, PermissionDecisionReason
from harness.aether2.hooks.registry import HookRegistry


_UNSUPPORTED_MUTATION_MESSAGE = (
    "Permission hooks may not rewrite tool arguments; in-place argument mutation is not supported."
)


@dataclass(frozen=True)
class PermissionRule:
    """Permission rule: a tool-name matcher with an optional argument-content matcher."""

    behavior: str
    tool_matcher: str
    pattern: str | None = None
    argument_key: str | None = None
    source: str = "session"
    rule_id: str | None = None
    message: str | None = None

    def matches(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        tool_names = [part.strip() for part in self.tool_matcher.split("|") if part.strip()]
        if tool_names and "*" not in tool_names and tool_name not in tool_names:
            return False
        if self.argument_key is None or self.pattern is None:
            return True
        value = arguments.get(self.argument_key)
        if not isinstance(value, str):
            return False
        if any(char in self.pattern for char in "*?[]"):
            return fnmatch(value, self.pattern)
        return self.pattern in value


@dataclass(frozen=True)
class PermissionPolicy:
    allow: tuple[PermissionRule, ...] = ()
    deny: tuple[PermissionRule, ...] = ()
    ask: tuple[PermissionRule, ...] = ()
    default_behavior: str = "allow"


@dataclass(frozen=True)
class PermissionAudit:
    decision: PermissionDecision
    matched_rule: PermissionRule | None = None
    hook_invocations: list[HookInvocation] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.as_dict(),
            "matched_rule": None if self.matched_rule is None else self.matched_rule.__dict__.copy(),
            "hook_invocations": [invocation.as_dict() for invocation in self.hook_invocations],
        }


class PermissionManager:
    """Policy and hook coordinator for tool permission decisions."""

    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self.policy = policy or PermissionPolicy()

    def authorize(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        hook_registry: HookRegistry | None,
        workspace_root: str | Path | None = None,
        call_id: str | None = None,
    ) -> PermissionAudit:
        hook_invocations: list[HookInvocation] = []
        if hook_registry is not None:
            hook_result = hook_registry.run(
                "permission_request",
                HookContext.create(
                    event="permission_request",
                    tool_name=tool_name,
                    arguments=arguments,
                    call_id=call_id,
                    workspace_root=None if workspace_root is None else str(workspace_root),
                ),
            )
            hook_invocations.extend(hook_result.invocations)
            hook_decision = hook_result.permission_decision
            if hook_decision is not None:
                if hook_decision.updated_input not in (None, arguments):
                    hook_decision = PermissionDecision(
                        behavior="deny",
                        message=_UNSUPPORTED_MUTATION_MESSAGE,
                        reason=PermissionDecisionReason(
                            type="hook_mutation_unsupported",
                            source="hook",
                            message=_UNSUPPORTED_MUTATION_MESSAGE,
                        ),
                    )
                elif hook_decision.behavior == "ask":
                    hook_decision = PermissionDecision(
                        behavior="deny",
                        message=hook_decision.message or "Permission request requires interactive approval",
                        reason=PermissionDecisionReason(
                            type="ask_unavailable",
                            source="hook",
                            message=hook_decision.message,
                        ),
                    )
                return PermissionAudit(decision=hook_decision, matched_rule=None, hook_invocations=hook_invocations)

        for behavior_name in ("deny", "ask", "allow"):
            rules = getattr(self.policy, behavior_name)
            for rule in rules:
                if not rule.matches(tool_name, arguments):
                    continue
                message = rule.message
                if behavior_name == "allow":
                    decision = PermissionDecision(
                        behavior="allow",
                        reason=PermissionDecisionReason(
                            type="rule",
                            source=rule.source,
                            rule_id=rule.rule_id,
                            message=message,
                        ),
                    )
                else:
                    deny_message = message or self._default_message(tool_name, behavior_name, rule, arguments)
                    decision = PermissionDecision(
                        behavior="deny" if behavior_name == "ask" else behavior_name,
                        message=deny_message,
                        reason=PermissionDecisionReason(
                            type="rule",
                            source=rule.source,
                            rule_id=rule.rule_id,
                            message=deny_message,
                        ),
                        metadata={"matched_behavior": behavior_name},
                    )
                return PermissionAudit(decision=decision, matched_rule=rule, hook_invocations=hook_invocations)

        default_behavior = self.policy.default_behavior
        if default_behavior == "allow":
            return PermissionAudit(
                decision=PermissionDecision(
                    behavior="allow",
                    reason=PermissionDecisionReason(type="default", source="policy"),
                ),
                hook_invocations=hook_invocations,
            )
        default_message = f"Permission denied for tool {tool_name} by default policy"
        return PermissionAudit(
            decision=PermissionDecision(
                behavior="deny",
                message=default_message,
                reason=PermissionDecisionReason(
                    type="default",
                    source="policy",
                    message=default_message,
                ),
            ),
            hook_invocations=hook_invocations,
        )

    def build_denied_observation(self, ctx: Any, *, tool_name: str, arguments: dict[str, Any], audit: PermissionAudit) -> Any:
        raw = {
            "tool": tool_name,
            "exit_code": 1,
            "duration_sec": 0.0,
            "cwd": str(ctx.executor.workspace_root),
            "stdout": "",
            "stderr": audit.decision.message or "Permission denied",
            "error": {
                "kind": "permission_denied",
                "message": audit.decision.message or "Permission denied",
                "reason_code": "tool_permission_denied",
                "failure_class": "permission",
                "details": json.dumps(
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "permission_audit": audit.as_dict(),
                    },
                    sort_keys=True,
                ),
                "tool_name": tool_name,
            },
        }
        return ctx.observe_synthetic(raw)

    def _default_message(
        self,
        tool_name: str,
        behavior_name: str,
        rule: PermissionRule,
        arguments: dict[str, Any],
    ) -> str:
        if rule.argument_key and isinstance(arguments.get(rule.argument_key), str):
            value = arguments[rule.argument_key]
            return f"Permission denied for {tool_name} with {rule.argument_key}={value!r}"
        if behavior_name == "ask":
            return f"Permission request for {tool_name} requires interactive approval"
        return f"Permission denied for tool {tool_name}"


__all__ = ["PermissionAudit", "PermissionManager", "PermissionPolicy", "PermissionRule"]
