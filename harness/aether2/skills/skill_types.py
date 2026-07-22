"""Skill dataclasses and hook metadata types for the Aether runtime.

Extracted from loader.py to keep that module under 500 LOC.
All public types are re-exported from skills/loader.py for backward compat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping
import json

from harness.aether2.skills.frontmatter_helpers import (
    _coerce_model,
    _coerce_optional_string,
    _extract_description_from_markdown,
    _parse_argument_names,
    _parse_bool,
    _parse_simple_yaml,
    _parse_skill_paths,
    _parse_string_list,
    _FRONTMATTER_BOUNDARY,
)

LoadedFrom = Literal["skills", "bundled", "mcp", "plugin", "managed", "commands_DEPRECATED"]
SkillExecutionContext = Literal["inline", "fork"]
SkillSource = Literal["policySettings", "userSettings", "projectSettings", "plugin", "bundled", "mcp"]
SkillHookEvent = Literal["permission_request", "pre_tool_use", "post_tool_use"]


@dataclass(frozen=True)
class SkillLoadIssue:
    reason_code: str
    message: str
    skill_name: str | None = None
    file_path: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "skill_name": self.skill_name,
            "file_path": self.file_path,
            "source": self.source,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class SkillHookCommand:
    raw: str | dict[str, Any]
    once: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"once": self.once}
        if isinstance(self.raw, str):
            payload["command"] = self.raw
        else:
            payload["command"] = json.loads(json.dumps(self.raw, sort_keys=True, ensure_ascii=True))
        return payload


@dataclass(frozen=True)
class SkillHookMatcher:
    original_event: str
    normalized_event: SkillHookEvent | None
    matcher: str
    hooks: tuple[SkillHookCommand, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_event": self.original_event,
            "normalized_event": self.normalized_event,
            "matcher": self.matcher,
            "hooks": [hook.as_dict() for hook in self.hooks],
        }


@dataclass(frozen=True)
class SkillHookMetadata:
    matchers: tuple[SkillHookMatcher, ...] = ()
    unsupported_events: tuple[str, ...] = ()

    @property
    def has_supported_hooks(self) -> bool:
        return any(matcher.normalized_event is not None and matcher.hooks for matcher in self.matchers)

    def as_dict(self) -> dict[str, Any]:
        return {
            "matchers": [matcher.as_dict() for matcher in self.matchers],
            "unsupported_events": list(self.unsupported_events),
        }


@dataclass(frozen=True)
class ParsedSkillFrontmatter:
    display_name: str | None
    description: str
    has_user_specified_description: bool
    allowed_tools: tuple[str, ...]
    argument_hint: str | None
    argument_names: tuple[str, ...]
    when_to_use: str | None
    version: str | None
    model: str | None
    disable_model_invocation: bool
    user_invocable: bool
    hooks: SkillHookMetadata
    execution_context: SkillExecutionContext | None
    agent: str | None
    effort: str | int | None
    shell: str | dict[str, Any] | None
    paths: tuple[str, ...] | None
    issues: tuple[SkillLoadIssue, ...] = ()


def parse_frontmatter_document(
    text: str,
    file_path: str,
) -> tuple[dict[str, Any], str, tuple[SkillLoadIssue, ...]]:
    """Parse a YAML frontmatter block from a markdown document."""
    if not text.startswith(f"{_FRONTMATTER_BOUNDARY}\n"):
        return {}, text, ()
    lines = text.splitlines()
    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_BOUNDARY:
            closing_index = index
            break
    if closing_index is None:
        return {}, text, (
            SkillLoadIssue(
                reason_code="invalid_frontmatter",
                message="Frontmatter starts with '---' but has no closing delimiter",
                file_path=file_path,
            ),
        )
    raw_frontmatter = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1:]).lstrip("\n")
    try:
        parsed = _parse_simple_yaml(raw_frontmatter) if raw_frontmatter.strip() else {}
    except ValueError as exc:
        return {}, body, (
            SkillLoadIssue(
                reason_code="invalid_frontmatter",
                message=f"Failed to parse YAML frontmatter: {exc}",
                file_path=file_path,
            ),
        )
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, Mapping):
        return {}, body, (
            SkillLoadIssue(
                reason_code="invalid_frontmatter",
                message="Skill frontmatter must parse to a mapping",
                file_path=file_path,
            ),
        )
    return dict(parsed), body, ()


_HOOK_EVENT_ALIASES = {
    "PermissionRequest": "permission_request",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "permission_request": "permission_request",
    "pre_tool_use": "pre_tool_use",
    "post_tool_use": "post_tool_use",
}
_SUPPORTED_HOOK_EVENTS = tuple(_HOOK_EVENT_ALIASES)


def parse_hooks_from_frontmatter(
    value: Any,
    resolved_name: str,
) -> tuple[SkillHookMetadata, tuple[SkillLoadIssue, ...]]:
    """Parse the hooks mapping from skill frontmatter."""
    if value is None:
        return SkillHookMetadata(), ()
    if not isinstance(value, Mapping):
        return SkillHookMetadata(), (
            SkillLoadIssue(
                reason_code="invalid_hooks_metadata",
                message="hooks frontmatter must be a mapping",
                skill_name=resolved_name,
            ),
        )

    issues: list[SkillLoadIssue] = []
    matchers: list[SkillHookMatcher] = []
    unsupported_events: list[str] = []
    for event_name in sorted(value):
        normalized_event = _HOOK_EVENT_ALIASES.get(str(event_name))
        if normalized_event is None:
            unsupported_events.append(str(event_name))
            issues.append(
                SkillLoadIssue(
                    reason_code="unsupported_skill_hook_event",
                    message=f"Unsupported skill hook event '{event_name}' for the current Aether hook substrate",
                    skill_name=resolved_name,
                    metadata={"supported_events": list(_SUPPORTED_HOOK_EVENTS)},
                )
            )
        raw_matchers = value[event_name]
        if not isinstance(raw_matchers, list):
            issues.append(
                SkillLoadIssue(
                    reason_code="invalid_hooks_metadata",
                    message=f"hooks.{event_name} must be a list of matcher entries",
                    skill_name=resolved_name,
                )
            )
            continue
        for index, raw_matcher in enumerate(raw_matchers):
            if not isinstance(raw_matcher, Mapping):
                issues.append(
                    SkillLoadIssue(
                        reason_code="invalid_hooks_metadata",
                        message=f"hooks.{event_name}[{index}] must be a mapping",
                        skill_name=resolved_name,
                    )
                )
                continue
            matcher_text = _coerce_optional_string(raw_matcher.get("matcher")) or ""
            raw_hooks = raw_matcher.get("hooks")
            if not isinstance(raw_hooks, list) or not raw_hooks:
                issues.append(
                    SkillLoadIssue(
                        reason_code="invalid_hooks_metadata",
                        message=f"hooks.{event_name}[{index}].hooks must be a non-empty list",
                        skill_name=resolved_name,
                    )
                )
                continue
            hook_commands: list[SkillHookCommand] = []
            for raw_hook in raw_hooks:
                if isinstance(raw_hook, str):
                    hook_commands.append(SkillHookCommand(raw=raw_hook))
                    continue
                if isinstance(raw_hook, Mapping):
                    hook_commands.append(
                        SkillHookCommand(
                            raw=json.loads(json.dumps(dict(raw_hook), sort_keys=True, ensure_ascii=True)),
                            once=_parse_bool(raw_hook.get("once"), default=False),
                        )
                    )
                    continue
                issues.append(
                    SkillLoadIssue(
                        reason_code="invalid_hooks_metadata",
                        message=f"hooks.{event_name}[{index}].hooks entries must be strings or mappings",
                        skill_name=resolved_name,
                    )
                )
            if hook_commands:
                matchers.append(
                    SkillHookMatcher(
                        original_event=str(event_name),
                        normalized_event=normalized_event,
                        matcher=matcher_text,
                        hooks=tuple(hook_commands),
                    )
                )
    return SkillHookMetadata(matchers=tuple(matchers), unsupported_events=tuple(unsupported_events)), tuple(issues)


def parse_skill_frontmatter_fields(
    frontmatter: Mapping[str, Any],
    markdown_content: str,
    resolved_name: str,
    description_fallback_label: str = "Skill",
) -> ParsedSkillFrontmatter:
    """Parse all skill frontmatter fields into a ParsedSkillFrontmatter value."""
    issues: list[SkillLoadIssue] = []
    description_value = frontmatter.get("description")
    user_description: str | None = None
    if description_value is not None:
        if isinstance(description_value, str):
            user_description = description_value.strip()
        else:
            issues.append(
                SkillLoadIssue(
                    reason_code="invalid_description_frontmatter",
                    message="description frontmatter must be a string",
                    skill_name=resolved_name,
                )
            )
    description = user_description or _extract_description_from_markdown(markdown_content, description_fallback_label)

    hooks, hook_issues = parse_hooks_from_frontmatter(frontmatter.get("hooks"), resolved_name)
    issues.extend(hook_issues)

    effort = frontmatter.get("effort")
    if effort is not None and not isinstance(effort, (str, int)):
        issues.append(
            SkillLoadIssue(
                reason_code="invalid_effort_frontmatter",
                message="effort frontmatter must be a string or integer",
                skill_name=resolved_name,
            )
        )
        effort = None

    execution_context = "fork" if frontmatter.get("context") == "fork" else None
    shell_value = frontmatter.get("shell")
    if shell_value is not None and not isinstance(shell_value, (str, Mapping)):
        issues.append(
            SkillLoadIssue(
                reason_code="invalid_shell_frontmatter",
                message="shell frontmatter must be a string or mapping",
                skill_name=resolved_name,
            )
        )
        shell_value = None

    return ParsedSkillFrontmatter(
        display_name=_coerce_optional_string(frontmatter.get("name")),
        description=description,
        has_user_specified_description=user_description is not None,
        allowed_tools=tuple(_parse_string_list(frontmatter.get("allowed-tools"), delimiter=",")),
        argument_hint=_coerce_optional_string(frontmatter.get("argument-hint")),
        argument_names=tuple(_parse_argument_names(frontmatter.get("arguments"))),
        when_to_use=_coerce_optional_string(frontmatter.get("when_to_use")),
        version=_coerce_optional_string(frontmatter.get("version")),
        model=_coerce_model(frontmatter.get("model")),
        disable_model_invocation=_parse_bool(frontmatter.get("disable-model-invocation"), default=False),
        user_invocable=_parse_bool(frontmatter.get("user-invocable"), default=True),
        hooks=hooks,
        execution_context=execution_context,
        agent=_coerce_optional_string(frontmatter.get("agent")),
        effort=effort,
        shell=json.loads(json.dumps(shell_value, sort_keys=True, ensure_ascii=True))
        if isinstance(shell_value, Mapping)
        else shell_value,
        paths=_parse_skill_paths(frontmatter.get("paths")),
        issues=tuple(issues),
    )
