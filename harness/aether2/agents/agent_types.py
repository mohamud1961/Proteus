"""Agent dataclasses and parsing helpers extracted from agents/loader.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping
import json
import re

from harness.aether2.skills.skill_types import (
    SkillHookCommand,
    SkillHookMatcher,
    SkillHookMetadata,
)
from harness.aether2.tools.mcp import McpServerConfig


AgentSource = Literal["built-in", "flagSettings", "plugin", "policySettings", "projectSettings", "userSettings"]
AgentLoadedFrom = Literal["agents", "built-in", "plugin"]
AgentIsolation = Literal["worktree", "remote"]
AgentMemoryScope = Literal["local", "project", "user"]

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}
_SUPPORTED_HOOK_EVENTS = {"PermissionRequest", "PostToolUse", "PreToolUse", "permission_request", "post_tool_use", "pre_tool_use"}
_HOOK_EVENT_ALIASES = {
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreToolUse": "pre_tool_use",
    "permission_request": "permission_request",
    "post_tool_use": "post_tool_use",
    "pre_tool_use": "pre_tool_use",
}
_KNOWN_MCP_TRANSPORTS = {"fake_local", "http", "sdk", "sse", "stdio"}
_KNOWN_MCP_SCOPES = {"dynamic", "enterprise", "local", "managed", "project", "user"}
_KNOWN_MEMORY_SCOPES = {"local", "project", "user"}
_KNOWN_ISOLATION_MODES = {"remote", "worktree"}


@dataclass(frozen=True)
class AgentLoadIssue:
    reason_code: str
    message: str
    agent_type: str | None = None
    file_path: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "agent_type": self.agent_type,
            "file_path": self.file_path,
            "source": self.source,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class AgentMcpServerRef:
    name: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "reference", "name": self.name}


@dataclass(frozen=True)
class AgentInlineMcpServer:
    name: str
    config: McpServerConfig

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "inline",
            "name": self.name,
            "config": {
                "type": self.config.type,
                "scope": self.config.scope,
                "command": self.config.command,
                "args": list(self.config.args),
                "url": self.config.url,
                "env": dict(self.config.env),
                "headers": dict(self.config.headers),
                "timeout_sec": self.config.timeout_sec,
            },
        }


AgentMcpServerSpec = AgentMcpServerRef | AgentInlineMcpServer


@dataclass(frozen=True)
class ParsedAgentFrontmatter:
    agent_type: str | None
    when_to_use: str | None
    tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    skills: tuple[str, ...]
    mcp_servers: tuple[AgentMcpServerSpec, ...]
    required_mcp_servers: tuple[str, ...]
    hooks: SkillHookMetadata
    color: str | None
    model: str | None
    effort: str | int | None
    permission_mode: str | None
    max_turns: int | None
    background: bool
    initial_prompt: str | None
    memory: AgentMemoryScope | None
    isolation: AgentIsolation | None
    critical_system_reminder: str | None
    omit_claude_md: bool
    issues: tuple[AgentLoadIssue, ...] = ()


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
    return default


def _parse_string_list(value: Any, *, delimiter: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for chunk in value.splitlines() for part in chunk.split(delimiter)]
        return [part for part in parts if part]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    items.append(stripped)
        return items
    return []


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _coerce_model(value: Any) -> str | None:
    model = _coerce_optional_string(value)
    if model is None:
        return None
    return None if model.casefold() == "inherit" else model


def _parse_positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _coerce_timeout(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _coerce_string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, str):
            result[key] = item
    return result


def _coerce_mcp_config(value: Mapping[str, Any]) -> McpServerConfig | None:
    transport = _coerce_optional_string(value.get("type"))
    if transport is None or transport not in _KNOWN_MCP_TRANSPORTS:
        return None
    scope = _coerce_optional_string(value.get("scope")) or "dynamic"
    if scope not in _KNOWN_MCP_SCOPES:
        scope = "dynamic"
    args = tuple(_parse_string_list(value.get("args"), delimiter=","))
    env = _coerce_string_mapping(value.get("env"))
    headers = _coerce_string_mapping(value.get("headers"))
    timeout_sec = _coerce_timeout(value.get("timeout_sec"))
    return McpServerConfig(
        type=transport,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        command=_coerce_optional_string(value.get("command")),
        args=args,
        url=_coerce_optional_string(value.get("url")),
        env=env,
        headers=headers,
        timeout_sec=timeout_sec,
    )


def _parse_mcp_servers(
    value: Any,
    agent_type: str | None,
    file_path: str,
    source: AgentSource,
) -> tuple[list[AgentMcpServerSpec], list[AgentLoadIssue]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [
            AgentLoadIssue(
                reason_code="invalid_agent_mcp_servers",
                message="mcpServers must be a list of server references or inline definitions",
                agent_type=agent_type,
                file_path=file_path,
                source=source,
            )
        ]
    specs: list[AgentMcpServerSpec] = []
    issues: list[AgentLoadIssue] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            name = item.strip()
            if name:
                specs.append(AgentMcpServerRef(name=name))
                continue
        elif isinstance(item, Mapping) and len(item) == 1:
            name, config_payload = next(iter(item.items()))
            if isinstance(name, str) and name.strip() and isinstance(config_payload, Mapping):
                config = _coerce_mcp_config(config_payload)
                if config is None:
                    issues.append(
                        AgentLoadIssue(
                            reason_code="invalid_agent_mcp_server_config",
                            message=f"mcpServers[{index}] inline config is invalid",
                            agent_type=agent_type,
                            file_path=file_path,
                            source=source,
                            metadata={"server_name": name},
                        )
                    )
                else:
                    specs.append(AgentInlineMcpServer(name=name, config=config))
                continue
        issues.append(
            AgentLoadIssue(
                reason_code="invalid_agent_mcp_servers",
                message=f"mcpServers[{index}] must be a server name or a single-entry mapping",
                agent_type=agent_type,
                file_path=file_path,
                source=source,
            )
        )
    return specs, issues


def _parse_hooks_from_frontmatter(
    value: Any,
    agent_type: str | None,
    file_path: str,
    source: AgentSource,
) -> tuple[SkillHookMetadata, tuple[AgentLoadIssue, ...]]:
    if value is None:
        return SkillHookMetadata(), ()
    if not isinstance(value, Mapping):
        return SkillHookMetadata(), (
            AgentLoadIssue(
                reason_code="invalid_agent_hooks_metadata",
                message="hooks frontmatter must be a mapping",
                agent_type=agent_type,
                file_path=file_path,
                source=source,
            ),
        )
    issues: list[AgentLoadIssue] = []
    matchers: list[SkillHookMatcher] = []
    unsupported_events: list[str] = []
    for event_name in sorted(value):
        normalized_event = _HOOK_EVENT_ALIASES.get(str(event_name))
        if normalized_event is None:
            unsupported_events.append(str(event_name))
            issues.append(
                AgentLoadIssue(
                    reason_code="unsupported_agent_hook_event",
                    message=f"Unsupported agent hook event '{event_name}' for the current Aether hook substrate",
                    agent_type=agent_type,
                    file_path=file_path,
                    source=source,
                    metadata={"supported_events": sorted(_SUPPORTED_HOOK_EVENTS)},
                )
            )
        raw_matchers = value[event_name]
        if not isinstance(raw_matchers, list):
            issues.append(
                AgentLoadIssue(
                    reason_code="invalid_agent_hooks_metadata",
                    message=f"hooks.{event_name} must be a list of matcher entries",
                    agent_type=agent_type,
                    file_path=file_path,
                    source=source,
                )
            )
            continue
        for index, raw_matcher in enumerate(raw_matchers):
            if not isinstance(raw_matcher, Mapping):
                issues.append(
                    AgentLoadIssue(
                        reason_code="invalid_agent_hooks_metadata",
                        message=f"hooks.{event_name}[{index}] must be a mapping",
                        agent_type=agent_type,
                        file_path=file_path,
                        source=source,
                    )
                )
                continue
            matcher_text = _coerce_optional_string(raw_matcher.get("matcher")) or ""
            raw_hooks = raw_matcher.get("hooks")
            if not isinstance(raw_hooks, list) or not raw_hooks:
                issues.append(
                    AgentLoadIssue(
                        reason_code="invalid_agent_hooks_metadata",
                        message=f"hooks.{event_name}[{index}].hooks must be a non-empty list",
                        agent_type=agent_type,
                        file_path=file_path,
                        source=source,
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
                    AgentLoadIssue(
                        reason_code="invalid_agent_hooks_metadata",
                        message=f"hooks.{event_name}[{index}].hooks entries must be strings or mappings",
                        agent_type=agent_type,
                        file_path=file_path,
                        source=source,
                    )
                )
            if hook_commands:
                matchers.append(
                    SkillHookMatcher(
                        original_event=str(event_name),
                        normalized_event=normalized_event,  # type: ignore[arg-type]
                        matcher=matcher_text,
                        hooks=tuple(hook_commands),
                    )
                )
    return SkillHookMetadata(matchers=tuple(matchers), unsupported_events=tuple(unsupported_events)), tuple(issues)


def parse_agent_frontmatter_fields(
    frontmatter: Mapping[str, Any],
    markdown_content: str,
    file_path: str,
    *,
    source: AgentSource,
) -> ParsedAgentFrontmatter:
    """Parse all agent frontmatter fields into a ParsedAgentFrontmatter value."""
    del markdown_content
    issues: list[AgentLoadIssue] = []
    agent_type = _coerce_optional_string(frontmatter.get("name"))
    if frontmatter.get("name") is not None and agent_type is None:
        issues.append(
            AgentLoadIssue(
                reason_code="invalid_agent_name",
                message="Agent name must be a non-empty string",
                file_path=file_path,
                source=source,
            )
        )
    description_value = frontmatter.get("description")
    when_to_use: str | None = None
    if description_value is not None:
        if isinstance(description_value, str) and description_value.strip():
            when_to_use = description_value.replace("\\n", "\n").strip()
        else:
            issues.append(
                AgentLoadIssue(
                    reason_code="invalid_agent_description",
                    message="Agent description must be a non-empty string",
                    agent_type=agent_type,
                    file_path=file_path,
                    source=source,
                )
            )

    mcp_servers, mcp_issues = _parse_mcp_servers(frontmatter.get("mcpServers"), agent_type, file_path, source)
    issues.extend(mcp_issues)
    hooks, hook_issues = _parse_hooks_from_frontmatter(frontmatter.get("hooks"), agent_type, file_path, source)
    issues.extend(hook_issues)

    max_turns = _parse_positive_int(frontmatter.get("maxTurns"))
    if frontmatter.get("maxTurns") is not None and max_turns is None:
        issues.append(
            AgentLoadIssue(
                reason_code="invalid_agent_max_turns",
                message="maxTurns must be a positive integer",
                agent_type=agent_type,
                file_path=file_path,
                source=source,
            )
        )

    effort = frontmatter.get("effort")
    if effort is not None and not isinstance(effort, (int, str)):
        issues.append(
            AgentLoadIssue(
                reason_code="invalid_agent_effort",
                message="effort must be a string or integer",
                agent_type=agent_type,
                file_path=file_path,
                source=source,
            )
        )
        effort = None

    memory = _coerce_optional_string(frontmatter.get("memory"))
    if memory is not None and memory not in _KNOWN_MEMORY_SCOPES:
        issues.append(
            AgentLoadIssue(
                reason_code="invalid_agent_memory",
                message=f"memory must be one of {sorted(_KNOWN_MEMORY_SCOPES)}",
                agent_type=agent_type,
                file_path=file_path,
                source=source,
            )
        )
        memory = None

    isolation = _coerce_optional_string(frontmatter.get("isolation"))
    if isolation is not None and isolation not in _KNOWN_ISOLATION_MODES:
        issues.append(
            AgentLoadIssue(
                reason_code="invalid_agent_isolation",
                message=f"isolation must be one of {sorted(_KNOWN_ISOLATION_MODES)}",
                agent_type=agent_type,
                file_path=file_path,
                source=source,
            )
        )
        isolation = None

    return ParsedAgentFrontmatter(
        agent_type=agent_type,
        when_to_use=when_to_use,
        tools=tuple(_parse_string_list(frontmatter.get("tools"), delimiter=",")),
        disallowed_tools=tuple(_parse_string_list(frontmatter.get("disallowedTools"), delimiter=",")),
        skills=tuple(_parse_string_list(frontmatter.get("skills"), delimiter=",")),
        mcp_servers=tuple(mcp_servers),
        required_mcp_servers=tuple(_parse_string_list(frontmatter.get("requiredMcpServers"), delimiter=",")),
        hooks=hooks,
        color=_coerce_optional_string(frontmatter.get("color")),
        model=_coerce_model(frontmatter.get("model")),
        effort=effort,
        permission_mode=_coerce_optional_string(frontmatter.get("permissionMode")),
        max_turns=max_turns,
        background=_parse_bool(frontmatter.get("background"), default=False),
        initial_prompt=_coerce_optional_string(frontmatter.get("initialPrompt")),
        memory=memory,  # type: ignore[arg-type]
        isolation=isolation,  # type: ignore[arg-type]
        critical_system_reminder=_coerce_optional_string(frontmatter.get("criticalSystemReminder_EXPERIMENTAL")),
        omit_claude_md=_parse_bool(frontmatter.get("omitClaudeMd"), default=False),
        issues=tuple(issues),
    )
