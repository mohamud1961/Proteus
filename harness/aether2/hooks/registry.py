"""Session-scoped hook registry for the Aether harness."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from harness.aether2.hooks.lifecycle import HookContext, HookEvent, HookInvocation, HookResult, HookRunResult


class HookCallback(Protocol):
    def __call__(self, context: HookContext) -> HookResult | None:
        ...


@dataclass(frozen=True)
class RegisteredHook:
    event: HookEvent
    matcher: str
    hook_name: str
    callback: HookCallback
    source: str = "session"


def _matches(matcher: str, tool_name: str) -> bool:
    if not matcher or matcher == "*":
        return True
    parts = [part.strip() for part in matcher.split("|")]
    return any(part == tool_name for part in parts if part)


class HookRegistry:
    """Minimal in-memory registry for permission and tool lifecycle hooks."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[RegisteredHook]] = {
            "permission_request": [],
            "pre_tool_use": [],
            "post_tool_use": [],
        }

    def register(
        self,
        event: HookEvent,
        *,
        matcher: str = "",
        hook_name: str | None = None,
        callback: HookCallback,
        source: str = "session",
    ) -> RegisteredHook:
        registration = RegisteredHook(
            event=event,
            matcher=matcher,
            hook_name=hook_name or f"{event}_hook_{len(self._hooks[event]) + 1}",
            callback=callback,
            source=source,
        )
        self._hooks[event].append(registration)
        return registration

    def clear(self) -> None:
        for registrations in self._hooks.values():
            registrations.clear()

    def run(self, event: HookEvent, context: HookContext) -> HookRunResult:
        invocations: list[HookInvocation] = []
        decision = None

        for registration in self._hooks[event]:
            if not _matches(registration.matcher, context.tool_name):
                continue
            started_at = perf_counter()
            try:
                result = registration.callback(context)
                duration_sec = perf_counter() - started_at
                if result is not None and result.permission_decision is not None and decision is None:
                    decision = result.permission_decision
                invocations.append(
                    HookInvocation(
                        hook_name=registration.hook_name,
                        event=event,
                        matcher=registration.matcher,
                        status="matched",
                        duration_sec=duration_sec,
                        note=None if result is None else result.note,
                        decision=(
                            None
                            if result is None or result.permission_decision is None
                            else result.permission_decision.as_dict()
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - hooks must fail truthfully, not invisibly
                invocations.append(
                    HookInvocation(
                        hook_name=registration.hook_name,
                        event=event,
                        matcher=registration.matcher,
                        status="error",
                        duration_sec=perf_counter() - started_at,
                        error=str(exc),
                    )
                )

        return HookRunResult(invocations=invocations, permission_decision=decision)


__all__ = ["HookCallback", "HookRegistry", "RegisteredHook"]
