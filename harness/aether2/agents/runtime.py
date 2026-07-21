"""Explicit local/fake subagent runtime boundary for Aether-2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
import json

from harness.aether2.agents.handoff import WorkerHandoff
from harness.aether2.agents.loader import AgentDefinition, AgentMcpServerRef
from harness.aether2.agents.task import WorkerTaskPacket
from harness.aether2.skills.registry import SkillRegistry, SkillSelectionResult
from harness.aether2.tools.registry import ToolRegistry


@dataclass(frozen=True)
class AgentRuntimeIssue:
    reason_code: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class ResolvedSkillReference:
    ref: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "status": self.status,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class ResolvedMcpServerReference:
    name: str
    status: str
    matched_tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "matched_tools": list(self.matched_tools),
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class PreparedAgentRun:
    agent: AgentDefinition
    task_packet: WorkerTaskPacket
    selected_skills: SkillSelectionResult
    skill_resolution: tuple[ResolvedSkillReference, ...]
    mcp_resolution: tuple[ResolvedMcpServerReference, ...]
    resolved_tool_names: tuple[str, ...]
    issues: tuple[AgentRuntimeIssue, ...]
    execution_mode: str = "in_process_fake"
    background_execution_assumed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent.as_dict(),
            "task_packet": self.task_packet.as_dict(),
            "selected_skills": [skill.as_dict() for skill in self.selected_skills.skills],
            "skill_issues": [issue.as_dict() for issue in self.selected_skills.issues],
            "skill_resolution": [item.as_dict() for item in self.skill_resolution],
            "mcp_resolution": [item.as_dict() for item in self.mcp_resolution],
            "resolved_tool_names": list(self.resolved_tool_names),
            "issues": [issue.as_dict() for issue in self.issues],
            "execution_mode": self.execution_mode,
            "background_execution_assumed": self.background_execution_assumed,
        }

    def parent_visible_risks(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issues)


@dataclass(frozen=True)
class SubagentRunResult:
    prepared: PreparedAgentRun
    handoff: WorkerHandoff

    def as_dict(self) -> dict[str, Any]:
        return {
            "prepared": self.prepared.as_dict(),
            "handoff": self.handoff.as_dict(),
            "parent_visible_risks": list(self.parent_visible_risks()),
        }

    def parent_visible_risks(self) -> tuple[str, ...]:
        visible = list(self.prepared.parent_visible_risks())
        visible.extend(self.handoff.parent_visible_items())
        return tuple(visible)


WorkerCallable = Callable[[PreparedAgentRun], WorkerHandoff]


class SubagentRuntime:
    """Prepare and execute explicit local/fake worker tasks with visible handoff boundaries."""

    def __init__(
        self,
        *,
        skill_registry: SkillRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.skill_registry = skill_registry
        self.tool_registry = tool_registry

    def prepare(self, agent: AgentDefinition, task_packet: WorkerTaskPacket) -> PreparedAgentRun:
        issues: list[AgentRuntimeIssue] = []
        if task_packet.agent_type != agent.agent_type:
            issues.append(
                AgentRuntimeIssue(
                    reason_code="agent_task_type_mismatch",
                    message=f"Task packet targets '{task_packet.agent_type}' but agent definition is '{agent.agent_type}'",
                )
            )

        selected_skills = self.skill_registry.select(task_packet.skill_refs) if self.skill_registry is not None else SkillSelectionResult()
        skill_resolution = self._resolve_skills(task_packet, selected_skills, issues)
        mcp_resolution = self._resolve_mcp(task_packet, issues)
        resolved_tool_names = self._resolve_tools(task_packet, issues)

        if task_packet.background_requested:
            issues.append(
                AgentRuntimeIssue(
                    reason_code="background_execution_not_supported",
                    message="Background execution is not supported in the explicit local/fake subagent slice",
                    metadata={"agent_type": agent.agent_type},
                )
            )

        return PreparedAgentRun(
            agent=agent,
            task_packet=task_packet,
            selected_skills=selected_skills,
            skill_resolution=tuple(skill_resolution),
            mcp_resolution=tuple(mcp_resolution),
            resolved_tool_names=tuple(resolved_tool_names),
            issues=tuple(issues),
        )

    def execute(self, agent: AgentDefinition, task_packet: WorkerTaskPacket, worker: WorkerCallable) -> SubagentRunResult:
        prepared = self.prepare(agent, task_packet)
        handoff = worker(prepared)
        return SubagentRunResult(prepared=prepared, handoff=handoff)

    def _resolve_skills(
        self,
        task_packet: WorkerTaskPacket,
        selected_skills: SkillSelectionResult,
        issues: list[AgentRuntimeIssue],
    ) -> list[ResolvedSkillReference]:
        if self.skill_registry is None:
            if task_packet.skill_refs:
                issues.append(
                    AgentRuntimeIssue(
                        reason_code="skill_registry_missing",
                        message="Skill references were provided but no skill registry was configured",
                    )
                )
            return [ResolvedSkillReference(ref=ref, status="unresolved_no_registry") for ref in task_packet.skill_refs]

        resolved = {skill.name for skill in selected_skills.skills}
        output: list[ResolvedSkillReference] = []
        for ref in task_packet.skill_refs:
            if ref in resolved:
                output.append(ResolvedSkillReference(ref=ref, status="resolved"))
                continue
            related_issues = [issue for issue in selected_skills.issues if issue.skill_name == ref]
            if related_issues:
                for issue in related_issues:
                    issues.append(
                        AgentRuntimeIssue(
                            reason_code=issue.reason_code,
                            message=issue.message,
                            metadata=issue.as_dict(),
                        )
                    )
                output.append(
                    ResolvedSkillReference(
                        ref=ref,
                        status="issue",
                        metadata={"reason_codes": [issue.reason_code for issue in related_issues]},
                    )
                )
                continue
            output.append(ResolvedSkillReference(ref=ref, status="missing"))
        return output

    def _resolve_mcp(
        self,
        task_packet: WorkerTaskPacket,
        issues: list[AgentRuntimeIssue],
    ) -> list[ResolvedMcpServerReference]:
        available_servers = tuple(self.tool_registry.server_names()) if self.tool_registry is not None else ()
        output: list[ResolvedMcpServerReference] = []
        for spec in task_packet.mcp_servers:
            if isinstance(spec, AgentMcpServerRef):
                matched_tools = tuple(self.tool_registry.tool_names_for_server(spec.name) if self.tool_registry is not None else ())
                if matched_tools:
                    output.append(ResolvedMcpServerReference(name=spec.name, status="resolved", matched_tools=matched_tools))
                else:
                    issues.append(
                        AgentRuntimeIssue(
                            reason_code="agent_mcp_server_missing",
                            message=f"MCP server reference '{spec.name}' was not available in the registry",
                            metadata={"server_name": spec.name},
                        )
                    )
                    output.append(ResolvedMcpServerReference(name=spec.name, status="missing"))
                continue

            matched_tools = tuple(self.tool_registry.tool_names_for_server(spec.name) if self.tool_registry is not None else ())
            if matched_tools:
                output.append(
                    ResolvedMcpServerReference(
                        name=spec.name,
                        status="resolved_inline",
                        matched_tools=matched_tools,
                        metadata={"config": spec.as_dict()["config"]},
                    )
                )
                continue
            issues.append(
                AgentRuntimeIssue(
                    reason_code="agent_inline_mcp_unresolved",
                    message=f"Inline MCP server '{spec.name}' was retained but not activated by the local/fake runtime",
                    metadata={"server_name": spec.name, "config": spec.as_dict()["config"]},
                )
            )
            output.append(
                ResolvedMcpServerReference(
                    name=spec.name,
                    status="retained_unresolved_inline",
                    metadata={"config": spec.as_dict()["config"]},
                )
            )

        for pattern in task_packet.required_mcp_servers:
            if any(pattern.casefold() in server.casefold() for server in available_servers):
                continue
            issues.append(
                AgentRuntimeIssue(
                    reason_code="required_mcp_server_missing",
                    message=f"Required MCP server pattern '{pattern}' did not match any available server",
                    metadata={"available_servers": list(available_servers)},
                )
            )
        return output

    def _resolve_tools(
        self,
        task_packet: WorkerTaskPacket,
        issues: list[AgentRuntimeIssue],
    ) -> list[str]:
        if self.tool_registry is None:
            if task_packet.allowed_tools:
                issues.append(
                    AgentRuntimeIssue(
                        reason_code="tool_registry_missing",
                        message="Allowed tools were provided but no tool registry was configured",
                    )
                )
            return list(task_packet.allowed_tools)
        available = set(self.tool_registry.tool_names(discoverable_only=False))
        resolved: list[str] = []
        for tool_name in task_packet.allowed_tools:
            if tool_name == "*":
                resolved.extend(sorted(available))
                continue
            if tool_name in available:
                resolved.append(tool_name)
                continue
            issues.append(
                AgentRuntimeIssue(
                    reason_code="agent_tool_not_found",
                    message=f"Allowed tool '{tool_name}' was not found in the registry",
                    metadata={"tool_name": tool_name},
                )
            )
        return resolved


__all__ = [
    "AgentRuntimeIssue",
    "PreparedAgentRun",
    "ResolvedMcpServerReference",
    "ResolvedSkillReference",
    "SubagentRunResult",
    "SubagentRuntime",
]
