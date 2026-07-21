"""Generic provider tool schemas and dispatch helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.aether2.hooks.lifecycle import HookContext, PermissionDecision, deep_copy_arguments
from harness.aether2.hooks.registry import HookRegistry
from harness.aether2.tools.permissions import PermissionManager

TOOL_NAMES: list[str] = [
    "run_command",
    "start_job",
    "job_status",
    "session_start",
    "session_send",
    "session_read",
    "read_file",
    "write_file",
    "wait",
    "task_done",
    "task_blocked",
    "query_evidence",
    "inspect_artifact",
]

LEGACY_TOOL_ALIASES: dict[str, str] = {"query_history": "query_evidence"}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "run_command",
        "Run a foreground command in the task container and return its typed observation.",
        {
            "cmd": {
                "type": "string",
                "description": "Shell command to execute in the task container.",
            },
            "timeout_sec": {
                "type": "integer",
                "default": 120,
                "minimum": 1,
                "description": "Maximum runtime in seconds.",
            },
            "cwd": {
                "type": ["string", "null"],
                "description": "Optional working directory inside the task container.",
            },
        },
        ["cmd"],
    ),
    _schema(
        "start_job",
        "Start a detached non-interactive job that keeps running after the current process exits. "
        "Use job_status for logs. session_send/session_read cannot attach to a start_job process; "
        "for programs that need stdin or live console input, start them with session_start instead.",
        {
            "cmd": {
                "type": "string",
                "description": "Shell command to launch as a detached job.",
            },
            "job_id": {
                "type": ["string", "null"],
                "description": "Optional stable identifier for the job registry entry.",
            },
            "cwd": {
                "type": ["string", "null"],
                "description": "Optional working directory for the detached job.",
            },
        },
        ["cmd"],
    ),
    _schema(
        "job_status",
        "Inspect a detached background job and report liveness and recent log tail.",
        {
            "job_id": {
                "type": "string",
                "description": "Identifier of the job registry entry to inspect.",
            },
        },
        ["job_id"],
    ),
    _schema(
        "session_start",
        "Start a new persistent interactive command session. This does not attach to a job that was "
        "already started with start_job; launch interactive programs here from the beginning when you "
        "will need session_send or session_read. The command must be the actual interactive program "
        "or a real connector to it; a dummy shell/cat proxy only receives your keystrokes and does not "
        "control another process.",
        {
            "session_id": {
                "type": "string",
                "description": "Identifier for the persistent session.",
            },
            "command": {
                "type": "string",
                "description": "Command to run inside the interactive session.",
            },
        },
        ["session_id", "command"],
    ),
    _schema(
        "session_send",
        "Send literal keystrokes or control sequences to an interactive session.",
        {
            "session_id": {
                "type": "string",
                "description": "Identifier of the session that receives the keystrokes.",
            },
            "keys": {
                "type": "string",
                "description": "Keys to send, including control sequences such as Enter or C-c.",
            },
        },
        ["session_id", "keys"],
    ),
    _schema(
        "session_read",
        "Read the current screen contents of an interactive session without advancing it.",
        {
            "session_id": {
                "type": "string",
                "description": "Identifier of the session to read.",
            },
        },
        ["session_id"],
    ),
    _schema(
        "read_file",
        "Read a bounded slice of a file from the task workspace. Reading a file you just wrote "
        "proves its contents and existence only, not that it satisfies the task's behavioral requirements.",
        {
            "path": {
                "type": "string",
                "description": "Path to the file to read.",
            },
            "offset": {
                "type": ["integer", "null"],
                "minimum": 0,
                "description": "Optional starting offset in bytes or lines, depending on the implementation.",
            },
            "limit": {
                "type": ["integer", "null"],
                "minimum": 0,
                "description": "Optional maximum amount to read in bytes or lines, depending on the implementation.",
            },
        },
        ["path"],
    ),
    _schema(
        "write_file",
        "Write file content atomically to the task workspace.",
        {
            "path": {
                "type": "string",
                "description": "Path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "File content to write atomically.",
            },
        },
        ["path", "content"],
    ),
    _schema(
        "wait",
        "Sleep without issuing a model call and surface any state changes after the pause.",
        {
            "seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": 300,
                "description": "Number of seconds to sleep, capped at 300.",
            },
            "reason": {
                "type": "string",
                "description": "Short reason for the wait.",
            },
        },
        ["seconds", "reason"],
    ),
    _schema(
        "task_done",
        "Claim completion and trigger verification with declared evidence checks.",
        {
            "summary": {
                "type": "string",
                "description": "Free-text completion claim.",
            },
            "checks": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "Shell commands used as evidence for the completion claim.",
            },
            "requirements": {
                "type": "array",
                "description": "Optional bounded requirement-evidence mappings for the verifier.",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement": {
                            "type": "string",
                            "description": "Requirement text grounded in the task contract.",
                        },
                        "requirement_id": {
                            "type": ["string", "null"],
                            "description": "Stable requirement identifier when one is available.",
                        },
                        "check": {
                            "type": ["string", "null"],
                            "description": "Declared check command that supports the requirement.",
                        },
                        "observation_ref": {
                            "type": ["string", "null"],
                            "description": "Visible observation or receipt ref that supports the requirement.",
                        },
                        "claimed_boundary": {
                            "type": ["string", "null"],
                            "description": "Explicit boundary or limit for the claim, if any.",
                        },
                        "known_limitations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Known limitations that keep the claim grounded.",
                        },
                    },
                    "required": ["requirement"],
                    "additionalProperties": False,
                },
            },
            "limitations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Known limitations for the overall completion claim.",
            },
        },
        ["summary", "checks"],
    ),
    _schema(
        "task_blocked",
        "Report an unresolved blocker with evidence instead of claiming completion.",
        {
            "blocker": {
                "type": "string",
                "description": "Short description of the blocking condition.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Visible evidence supporting the blocked claim.",
            },
            "attempts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bounded attempts already made against the blocker.",
            },
            "missing_external_state": {
                "type": "array",
                "items": {"type": "string"},
                "description": "External state that is still missing or unavailable.",
            },
            "recommended_next_evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Next evidence needed to revisit the blocker.",
            },
            "limitations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Known limitations or constraints that remain unresolved.",
            },
        },
        ["blocker", "evidence", "attempts", "missing_external_state", "recommended_next_evidence"],
    ),
    _schema(
        "query_evidence",
        "Search prior commands, outputs, artifacts, and observations from the current run by keyword.",
        {
            "query": {
                "type": "string",
                "description": "Substring or keyword to match against tool names, arguments, and output text.",
            },
            "tool": {
                "type": ["string", "null"],
                "description": "Optional tool name to restrict results to (e.g. 'run_command').",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum number of matching entries to return, most-recent-first.",
            },
        },
        ["query"],
    ),
    _schema(
        "inspect_artifact",
        "Inspect a task artifact generically by path. For PDFs and images, mode='auto' already attempts text extraction or OCR when available; use mode='metadata' only when you need file type or size rather than document contents.",
        {
            "path": {"type": "string", "description": "Path to inspect inside the task workspace."},
            "question": {"type": ["string", "null"], "description": "Optional question to guide the inspection summary, such as whether a document is an invoice or what total/VAT text is visible."},
            "mode": {
                "type": "string",
                "enum": ["auto", "text", "ocr", "vision", "pdf", "frames", "metadata"],
                "default": "auto",
                "description": "Inspection mode. Use auto for the default best-effort path, pdf for PDF text extraction, ocr for images, and metadata only for file properties. Unsupported optional modes degrade gracefully.",
            },
            "max_outputs": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of derived observations to return.",
            },
        },
        ["path"],
    ),
]


class ToolHandler(Protocol):
    def __call__(self, tool_name: str, arguments: dict[str, Any], ctx: Any) -> Any:
        ...


@dataclass(frozen=True)
class ToolDispatchOutcome:
    envelope: Any
    permission_decision: dict[str, Any] | None = None
    hook_trace: list[dict[str, Any]] = field(default_factory=list)


def dispatch(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
    """Dispatch a tool call to the matching method on ctx."""

    canonical_tool_name = LEGACY_TOOL_ALIASES.get(tool_name, tool_name)
    if canonical_tool_name not in TOOL_NAMES:
        raise KeyError(f"unknown tool: {tool_name}")
    handler = getattr(ctx, canonical_tool_name, None)
    if handler is None:
        raise AttributeError(f"context does not implement {canonical_tool_name}")
    # query_evidence accepts 'limit' as a positional-style kwarg; strip None so
    # the handler default applies when the model omits the optional argument.
    if canonical_tool_name == "query_evidence":
        cleaned: dict[str, Any] = {k: v for k, v in args.items() if v is not None}
        return handler(**cleaned)
    return handler(**args)


def dispatch_with_hooks(
    tool_name: str,
    args: dict[str, Any],
    ctx: Any,
    *,
    call_id: str | None = None,
    handler: ToolHandler | None = None,
) -> ToolDispatchOutcome:
    """Dispatch through permission checks and lifecycle hooks without changing tool schemas."""

    tool_arguments = deep_copy_arguments(args)
    hook_registry = getattr(ctx, "hook_registry", None)
    if hook_registry is not None and not isinstance(hook_registry, HookRegistry):
        raise TypeError("hook_registry must be a HookRegistry instance")
    permission_manager = getattr(ctx, "permission_manager", None)
    if permission_manager is None:
        permission_manager = PermissionManager()
    workspace_root = _workspace_root(ctx)

    permission_audit = permission_manager.authorize(
        tool_name=tool_name,
        arguments=tool_arguments,
        hook_registry=hook_registry,
        workspace_root=workspace_root,
        call_id=call_id,
    )
    hook_trace = [invocation.as_dict() for invocation in permission_audit.hook_invocations]
    permission_decision = permission_audit.decision.as_dict()

    if permission_audit.decision.behavior != "allow":
        envelope = permission_manager.build_denied_observation(
            ctx,
            tool_name=tool_name,
            arguments=tool_arguments,
            audit=permission_audit,
        )
        hook_trace.extend(
            _run_post_hooks(
                hook_registry,
                tool_name=tool_name,
                arguments=tool_arguments,
                call_id=call_id,
                workspace_root=workspace_root,
                permission_decision=permission_audit.decision,
                envelope=envelope,
            )
        )
        return ToolDispatchOutcome(
            envelope=envelope,
            permission_decision=permission_decision,
            hook_trace=hook_trace,
        )

    hook_trace.extend(
        _run_pre_hooks(
            hook_registry,
            tool_name=tool_name,
            arguments=tool_arguments,
            call_id=call_id,
            workspace_root=workspace_root,
            permission_decision=permission_audit.decision,
        )
    )
    dispatch_handler = dispatch if handler is None else handler
    envelope = dispatch_handler(tool_name, deep_copy_arguments(tool_arguments), ctx)
    hook_trace.extend(
        _run_post_hooks(
            hook_registry,
            tool_name=tool_name,
            arguments=tool_arguments,
            call_id=call_id,
            workspace_root=workspace_root,
            permission_decision=permission_audit.decision,
            envelope=envelope,
        )
    )
    return ToolDispatchOutcome(
        envelope=envelope,
        permission_decision=permission_decision,
        hook_trace=hook_trace,
    )


def _workspace_root(ctx: Any) -> str | None:
    executor = getattr(ctx, "executor", None)
    root = getattr(executor, "workspace_root", None)
    if root is None:
        return None
    return str(Path(root))


def _run_pre_hooks(
    hook_registry: HookRegistry | None,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str | None,
    workspace_root: str | None,
    permission_decision: PermissionDecision,
) -> list[dict[str, Any]]:
    if hook_registry is None:
        return []
    result = hook_registry.run(
        "pre_tool_use",
        HookContext.create(
            event="pre_tool_use",
            tool_name=tool_name,
            arguments=arguments,
            call_id=call_id,
            workspace_root=workspace_root,
            permission_decision=permission_decision,
        ),
    )
    return [invocation.as_dict() for invocation in result.invocations]


def _run_post_hooks(
    hook_registry: HookRegistry | None,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str | None,
    workspace_root: str | None,
    permission_decision: PermissionDecision,
    envelope: Any,
) -> list[dict[str, Any]]:
    if hook_registry is None:
        return []
    result = hook_registry.run(
        "post_tool_use",
        HookContext.create(
            event="post_tool_use",
            tool_name=tool_name,
            arguments=arguments,
            call_id=call_id,
            workspace_root=workspace_root,
            permission_decision=permission_decision,
            observation=envelope,
        ),
    )
    return [invocation.as_dict() for invocation in result.invocations]


__all__ = [
    "LEGACY_TOOL_ALIASES",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "ToolDispatchOutcome",
    "ToolHandler",
    "dispatch",
    "dispatch_with_hooks",
]
