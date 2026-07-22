"""Bounded worker task packets for the explicit Aether-2 subagent surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.aether2.agents.loader import AgentDefinition, AgentMcpServerSpec
from harness.aether2.skills.loader import SkillHookMetadata


@dataclass(frozen=True)
class TaskOwnership:
    owner: str
    writable_paths: tuple[str, ...]
    readonly_paths: tuple[str, ...] = ()
    notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "writable_paths": list(self.writable_paths),
            "readonly_paths": list(self.readonly_paths),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class WorkerTaskPacket:
    agent_type: str
    objective: str
    prompt: str
    scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    files_to_touch: tuple[str, ...]
    exit_criteria: tuple[str, ...]
    evidence_expectations: tuple[str, ...]
    ownership: TaskOwnership
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    mcp_servers: tuple[AgentMcpServerSpec, ...] = ()
    required_mcp_servers: tuple[str, ...] = ()
    permission_mode: str | None = None
    hook_metadata: SkillHookMetadata = field(default_factory=SkillHookMetadata)
    max_turns: int | None = None
    background_requested: bool = False
    initial_prompt: str | None = None
    external_state_policy: str = "Report any active external state in the worker handoff."
    handoff_required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "objective": self.objective,
            "prompt": self.prompt,
            "scope": list(self.scope),
            "out_of_scope": list(self.out_of_scope),
            "files_to_touch": list(self.files_to_touch),
            "exit_criteria": list(self.exit_criteria),
            "evidence_expectations": list(self.evidence_expectations),
            "ownership": self.ownership.as_dict(),
            "allowed_tools": list(self.allowed_tools),
            "disallowed_tools": list(self.disallowed_tools),
            "skill_refs": list(self.skill_refs),
            "mcp_servers": [spec.as_dict() for spec in self.mcp_servers],
            "required_mcp_servers": list(self.required_mcp_servers),
            "permission_mode": self.permission_mode,
            "hook_metadata": self.hook_metadata.as_dict(),
            "max_turns": self.max_turns,
            "background_requested": self.background_requested,
            "initial_prompt": self.initial_prompt,
            "external_state_policy": self.external_state_policy,
            "handoff_required": self.handoff_required,
        }


def create_worker_task_packet(
    agent: AgentDefinition,
    *,
    objective: str,
    prompt: str,
    scope: list[str] | tuple[str, ...],
    out_of_scope: list[str] | tuple[str, ...] = (),
    files_to_touch: list[str] | tuple[str, ...],
    exit_criteria: list[str] | tuple[str, ...],
    evidence_expectations: list[str] | tuple[str, ...],
    ownership: TaskOwnership,
) -> WorkerTaskPacket:
    if not objective.strip():
        raise ValueError("objective is required")
    if not prompt.strip():
        raise ValueError("prompt is required")
    if not tuple(scope):
        raise ValueError("scope must contain at least one item")
    if not tuple(files_to_touch):
        raise ValueError("files_to_touch must contain at least one path")
    if not tuple(exit_criteria):
        raise ValueError("exit_criteria must contain at least one item")
    if not tuple(evidence_expectations):
        raise ValueError("evidence_expectations must contain at least one item")
    return WorkerTaskPacket(
        agent_type=agent.agent_type,
        objective=objective.strip(),
        prompt=prompt.strip(),
        scope=tuple(str(item) for item in scope),
        out_of_scope=tuple(str(item) for item in out_of_scope),
        files_to_touch=tuple(str(item) for item in files_to_touch),
        exit_criteria=tuple(str(item) for item in exit_criteria),
        evidence_expectations=tuple(str(item) for item in evidence_expectations),
        ownership=ownership,
        allowed_tools=agent.tools,
        disallowed_tools=agent.disallowed_tools,
        skill_refs=agent.skills,
        mcp_servers=agent.mcp_servers,
        required_mcp_servers=agent.required_mcp_servers,
        permission_mode=agent.permission_mode,
        hook_metadata=agent.hooks,
        max_turns=agent.max_turns,
        background_requested=agent.background,
        initial_prompt=agent.initial_prompt,
    )


__all__ = ["TaskOwnership", "WorkerTaskPacket", "create_worker_task_packet"]
