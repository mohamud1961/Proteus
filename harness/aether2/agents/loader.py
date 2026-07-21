"""Agent loader implementation for the Aether harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping
import json

from harness.aether2.skills.loader import (
    SkillHookCommand,
    SkillHookMatcher,
    SkillHookMetadata,
    parse_frontmatter_document,
)
from harness.aether2.tools.mcp import McpServerConfig
from harness.aether2.agents.agent_types import (
    AgentIsolation,
    AgentLoadIssue,
    AgentLoadedFrom,
    AgentMemoryScope,
    AgentMcpServerRef,
    AgentMcpServerSpec,
    AgentInlineMcpServer,
    AgentSource,
    ParsedAgentFrontmatter,
    parse_agent_frontmatter_fields,
)

# Re-export public types for backward compatibility
__all__ = [
    "AgentDefinition",
    "AgentInlineMcpServer",
    "AgentIsolation",
    "AgentLoadIssue",
    "AgentLoadResult",
    "AgentLoadedFrom",
    "AgentMcpServerRef",
    "AgentMcpServerSpec",
    "AgentMemoryScope",
    "AgentSource",
    "ParsedAgentFrontmatter",
    "create_agent_definition",
    "filter_agents_by_mcp_requirements",
    "get_active_agents_from_list",
    "has_required_mcp_servers",
    "load_agents_from_directory",
    "parse_agent_frontmatter_fields",
]


@dataclass(frozen=True)
class AgentDefinition:
    agent_type: str
    when_to_use: str
    prompt: str
    source: AgentSource
    loaded_from: AgentLoadedFrom
    base_dir: str | None = None
    file_path: str | None = None
    canonical_file_path: str | None = None
    filename: str | None = None
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[AgentMcpServerSpec, ...] = ()
    required_mcp_servers: tuple[str, ...] = ()
    hooks: SkillHookMetadata = field(default_factory=SkillHookMetadata)
    color: str | None = None
    model: str | None = None
    effort: str | int | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    background: bool = False
    initial_prompt: str | None = None
    memory: AgentMemoryScope | None = None
    isolation: AgentIsolation | None = None
    critical_system_reminder: str | None = None
    omit_claude_md: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "when_to_use": self.when_to_use,
            "prompt": self.prompt,
            "source": self.source,
            "loaded_from": self.loaded_from,
            "base_dir": self.base_dir,
            "file_path": self.file_path,
            "canonical_file_path": self.canonical_file_path,
            "filename": self.filename,
            "tools": list(self.tools),
            "disallowed_tools": list(self.disallowed_tools),
            "skills": list(self.skills),
            "mcp_servers": [spec.as_dict() for spec in self.mcp_servers],
            "required_mcp_servers": list(self.required_mcp_servers),
            "hooks": self.hooks.as_dict(),
            "color": self.color,
            "model": self.model,
            "effort": self.effort,
            "permission_mode": self.permission_mode,
            "max_turns": self.max_turns,
            "background": self.background,
            "initial_prompt": self.initial_prompt,
            "memory": self.memory,
            "isolation": self.isolation,
            "critical_system_reminder": self.critical_system_reminder,
            "omit_claude_md": self.omit_claude_md,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class AgentLoadResult:
    agents: tuple[AgentDefinition, ...] = ()
    issues: tuple[AgentLoadIssue, ...] = ()


def create_agent_definition(
    *,
    agent_type: str,
    when_to_use: str,
    markdown_content: str,
    source: AgentSource,
    loaded_from: AgentLoadedFrom,
    base_dir: str | None,
    file_path: str | None,
    tools: Iterable[str],
    disallowed_tools: Iterable[str],
    skills: Iterable[str],
    mcp_servers: Iterable[AgentMcpServerSpec],
    required_mcp_servers: Iterable[str],
    hooks: SkillHookMetadata,
    color: str | None,
    model: str | None,
    effort: str | int | None,
    permission_mode: str | None,
    max_turns: int | None,
    background: bool,
    initial_prompt: str | None,
    memory: AgentMemoryScope | None,
    isolation: AgentIsolation | None,
    critical_system_reminder: str | None,
    omit_claude_md: bool,
    metadata: Mapping[str, Any] | None = None,
) -> AgentDefinition:
    canonical_path: str | None = None
    filename: str | None = None
    if file_path is not None:
        filename = Path(file_path).stem
        try:
            canonical_path = str(Path(file_path).resolve(strict=True))
        except FileNotFoundError:
            canonical_path = None
    base_metadata = {
        "content_length": len(markdown_content),
        "has_mcp_refs": bool(tuple(mcp_servers)),
        "has_skill_refs": bool(tuple(skills)),
        "retains_hooks": hooks.has_supported_hooks,
        "retains_permission_mode": permission_mode is not None,
    }
    if metadata:
        base_metadata.update(json.loads(json.dumps(dict(metadata), sort_keys=True, ensure_ascii=True)))
    return AgentDefinition(
        agent_type=agent_type,
        when_to_use=when_to_use,
        prompt=markdown_content.strip(),
        source=source,
        loaded_from=loaded_from,
        base_dir=base_dir,
        file_path=file_path,
        canonical_file_path=canonical_path,
        filename=filename,
        tools=tuple(tools),
        disallowed_tools=tuple(disallowed_tools),
        skills=tuple(skills),
        mcp_servers=tuple(mcp_servers),
        required_mcp_servers=tuple(required_mcp_servers),
        hooks=hooks,
        color=color,
        model=model,
        effort=effort,
        permission_mode=permission_mode,
        max_turns=max_turns,
        background=background,
        initial_prompt=initial_prompt,
        memory=memory,
        isolation=isolation,
        critical_system_reminder=critical_system_reminder,
        omit_claude_md=omit_claude_md,
        metadata=base_metadata,
    )


def load_agents_from_directory(base_path: str | Path, source: AgentSource) -> AgentLoadResult:
    base_dir = Path(base_path)
    if not base_dir.exists():
        return AgentLoadResult(
            issues=(
                AgentLoadIssue(
                    reason_code="agents_directory_missing",
                    message="agents directory does not exist",
                    file_path=str(base_dir),
                    source=source,
                ),
            )
        )
    if not base_dir.is_dir():
        return AgentLoadResult(
            issues=(
                AgentLoadIssue(
                    reason_code="agents_directory_invalid",
                    message="agents base path is not a directory",
                    file_path=str(base_dir),
                    source=source,
                ),
            )
        )

    agents: list[AgentDefinition] = []
    issues: list[AgentLoadIssue] = []
    for entry in sorted(base_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or entry.suffix.lower() != ".md":
            continue
        try:
            raw_text = entry.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(
                AgentLoadIssue(
                    reason_code="agent_read_failed",
                    message=f"Failed to read agent file: {exc}",
                    file_path=str(entry),
                    source=source,
                )
            )
            continue
        frontmatter, content, parse_issues = parse_frontmatter_document(raw_text, str(entry))
        issues.extend(
            AgentLoadIssue(
                reason_code=issue.reason_code,
                message=issue.message,
                file_path=issue.file_path,
                source=source,
                metadata=dict(issue.metadata),
            )
            for issue in parse_issues
        )
        parsed = parse_agent_frontmatter_fields(frontmatter, content, str(entry), source=source)
        issues.extend(parsed.issues)
        if parsed.agent_type is None:
            continue
        if parsed.when_to_use is None:
            issues.append(
                AgentLoadIssue(
                    reason_code="agent_description_missing",
                    message="Agent frontmatter requires a description field",
                    agent_type=parsed.agent_type,
                    file_path=str(entry),
                    source=source,
                )
            )
            continue
        agents.append(
            create_agent_definition(
                agent_type=parsed.agent_type,
                when_to_use=parsed.when_to_use,
                markdown_content=content,
                source=source,
                loaded_from="agents",
                base_dir=str(base_dir.resolve()),
                file_path=str(entry),
                tools=parsed.tools,
                disallowed_tools=parsed.disallowed_tools,
                skills=parsed.skills,
                mcp_servers=parsed.mcp_servers,
                required_mcp_servers=parsed.required_mcp_servers,
                hooks=parsed.hooks,
                color=parsed.color,
                model=parsed.model,
                effort=parsed.effort,
                permission_mode=parsed.permission_mode,
                max_turns=parsed.max_turns,
                background=parsed.background,
                initial_prompt=parsed.initial_prompt,
                memory=parsed.memory,
                isolation=parsed.isolation,
                critical_system_reminder=parsed.critical_system_reminder,
                omit_claude_md=parsed.omit_claude_md,
            )
        )
    return AgentLoadResult(agents=tuple(agents), issues=tuple(issues))


def get_active_agents_from_list(all_agents: Iterable[AgentDefinition]) -> list[AgentDefinition]:
    groups = {
        "built-in": [],
        "plugin": [],
        "userSettings": [],
        "projectSettings": [],
        "flagSettings": [],
        "policySettings": [],
    }
    for agent in all_agents:
        groups.setdefault(agent.source, []).append(agent)
    winners: dict[str, AgentDefinition] = {}
    for source_name in ("built-in", "plugin", "userSettings", "projectSettings", "flagSettings", "policySettings"):
        for agent in groups.get(source_name, ()):
            winners[agent.agent_type] = agent
    return list(winners.values())


def has_required_mcp_servers(agent: AgentDefinition, available_servers: Iterable[str]) -> bool:
    available = tuple(available_servers)
    if not agent.required_mcp_servers:
        return True
    return all(any(pattern.casefold() in server.casefold() for server in available) for pattern in agent.required_mcp_servers)


def filter_agents_by_mcp_requirements(
    agents: Iterable[AgentDefinition],
    available_servers: Iterable[str],
) -> list[AgentDefinition]:
    available = tuple(available_servers)
    return [agent for agent in agents if has_required_mcp_servers(agent, available)]
