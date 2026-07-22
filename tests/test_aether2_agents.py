from __future__ import annotations

import harness.aether2 as public_api
from pathlib import Path

from harness.aether2.agents import (
    AgentDefinition,
    AgentInlineMcpServer,
    AgentMcpServerRef,
    ReviewRecord,
    SubagentRuntime,
    TaskOwnership,
    ValidationRecord,
    WorkerHandoff,
    create_agent_definition,
    create_worker_task_packet,
    filter_agents_by_mcp_requirements,
    get_active_agents_from_list,
    load_agents_from_directory,
)
from harness.aether2.skills import SkillHookMetadata, SkillRegistry, create_skill_spec
from harness.aether2.tools import (
    FakeLocalMcpServer,
    McpServerConfig,
    McpToolDescriptor,
    McpToolResult,
    build_mcp_tool_name,
    build_native_tool_registry,
    connect_fake_local_server,
)


def _write_agent(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _build_tool_registry():
    server = FakeLocalMcpServer(
        tools=[
            McpToolDescriptor(
                name="Echo Tool",
                description="Return the provided payload.",
                input_json_schema={
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                    "required": ["payload"],
                    "additionalProperties": False,
                },
            )
        ],
        handlers={"Echo Tool": lambda arguments, timeout_sec=None: McpToolResult(content=arguments["payload"])},
    )
    connection = connect_fake_local_server(
        "qa server",
        server,
        config=McpServerConfig(type="fake_local", timeout_sec=5),
    )
    return build_native_tool_registry().register_mcp_connection(connection)


def test_load_agents_from_directory_parses_frontmatter_and_retains_metadata(tmp_path: Path) -> None:
    agents_root = tmp_path / ".claude" / "agents"
    agents_root.mkdir(parents=True)
    _write_agent(
        agents_root / "alpha.md",
        """---
name: research-auditor
description: Audit repository state\\nand report gaps.
tools:
  - read_file
  - mcp__qa_server__Echo_Tool
disallowedTools:
  - write_file
skills:
  - alpha-skill
  - missing-skill
model: inherit
effort: 3
permissionMode: plan
maxTurns: 7
background: true
initialPrompt: Start with the failing trace.
memory: project
isolation: worktree
requiredMcpServers:
  - qa
  - logging
mcpServers:
  - qa server
  - inline server:
      type: fake_local
      timeout_sec: 9
hooks:
  PreToolUse:
    - matcher: read_file
      hooks:
        - command: python3 hooks/audit.py
          once: true
---
# Research Auditor

Audit the workspace and report blockers.
""",
    )
    _write_agent(
        agents_root / "beta.md",
        """---
name: support-runner
description: Run support diagnostics.
---
# Support Runner

Run the secondary checks.
""",
    )
    _write_agent(
        agents_root / "broken.md",
        """---
name: broken-agent
---
This file looks like an agent but is missing a description.
""",
    )
    _write_agent(agents_root / "notes.md", "Reference document only.\n")

    result = load_agents_from_directory(agents_root, "projectSettings")

    assert [agent.agent_type for agent in result.agents] == ["research-auditor", "support-runner"]
    alpha = result.agents[0]
    assert alpha.when_to_use == "Audit repository state\nand report gaps."
    assert alpha.tools == ("read_file", "mcp__qa_server__Echo_Tool")
    assert alpha.disallowed_tools == ("write_file",)
    assert alpha.skills == ("alpha-skill", "missing-skill")
    assert alpha.model is None
    assert alpha.effort == 3
    assert alpha.permission_mode == "plan"
    assert alpha.max_turns == 7
    assert alpha.background is True
    assert alpha.initial_prompt == "Start with the failing trace."
    assert alpha.memory == "project"
    assert alpha.isolation == "worktree"
    assert alpha.required_mcp_servers == ("qa", "logging")
    assert isinstance(alpha.mcp_servers[0], AgentMcpServerRef)
    assert isinstance(alpha.mcp_servers[1], AgentInlineMcpServer)
    assert alpha.hooks.has_supported_hooks is True
    assert alpha.hooks.matchers[0].normalized_event == "pre_tool_use"
    assert alpha.metadata["retains_permission_mode"] is True
    assert any(issue.reason_code == "agent_description_missing" for issue in result.issues)


def test_active_agent_precedence_and_required_mcp_filters_match_ts_shape() -> None:
    built_in = create_agent_definition(
        agent_type="reviewer",
        when_to_use="Built-in reviewer",
        markdown_content="Prompt",
        source="built-in",
        loaded_from="built-in",
        base_dir=None,
        file_path=None,
        tools=(),
        disallowed_tools=(),
        skills=(),
        mcp_servers=(),
        required_mcp_servers=("qa",),
        hooks=SkillHookMetadata(),
        color=None,
        model=None,
        effort=None,
        permission_mode=None,
        max_turns=None,
        background=False,
        initial_prompt=None,
        memory=None,
        isolation=None,
        critical_system_reminder=None,
        omit_claude_md=False,
    )
    policy = create_agent_definition(
        agent_type="reviewer",
        when_to_use="Policy reviewer",
        markdown_content="Prompt",
        source="policySettings",
        loaded_from="agents",
        base_dir=None,
        file_path=None,
        tools=(),
        disallowed_tools=(),
        skills=(),
        mcp_servers=(),
        required_mcp_servers=(),
        hooks=SkillHookMetadata(),
        color=None,
        model=None,
        effort=None,
        permission_mode=None,
        max_turns=None,
        background=False,
        initial_prompt=None,
        memory=None,
        isolation=None,
        critical_system_reminder=None,
        omit_claude_md=False,
    )

    active = get_active_agents_from_list([built_in, policy])
    assert len(active) == 1
    assert active[0].source == "policySettings"

    assert filter_agents_by_mcp_requirements(active, ["qa server"]) == active
    assert filter_agents_by_mcp_requirements([built_in], ["notes server"]) == []


def test_task_packet_and_runtime_keep_risks_visible_and_do_not_assume_background_execution(tmp_path: Path) -> None:
    skill = create_skill_spec(
        skill_name="alpha-skill",
        display_name=None,
        description="Alpha skill",
        has_user_specified_description=True,
        markdown_content="Alpha body",
        allowed_tools=("read_file",),
        argument_hint=None,
        argument_names=(),
        when_to_use="Use alpha.",
        version=None,
        model=None,
        disable_model_invocation=False,
        user_invocable=True,
        source="projectSettings",
        base_dir=str(tmp_path / "alpha"),
        loaded_from="skills",
        hooks=SkillHookMetadata(),
        execution_context=None,
        agent=None,
        paths=None,
        effort=None,
        shell=None,
    )
    skill_registry = SkillRegistry()
    skill_registry.register_skill(skill, source_precedence=0)

    tool_registry = _build_tool_registry()
    agent = create_agent_definition(
        agent_type="research-auditor",
        when_to_use="Audit repository state",
        markdown_content="Audit the workspace.",
        source="projectSettings",
        loaded_from="agents",
        base_dir=str(tmp_path),
        file_path=str(tmp_path / "alpha.md"),
        tools=("read_file", build_mcp_tool_name("qa server", "Echo Tool"), "missing_tool"),
        disallowed_tools=("write_file",),
        skills=("alpha-skill", "missing-skill"),
        mcp_servers=(
            AgentMcpServerRef(name="qa server"),
            AgentInlineMcpServer(name="inline server", config=McpServerConfig(type="fake_local", timeout_sec=5)),
        ),
        required_mcp_servers=("qa", "logs"),
        hooks=SkillHookMetadata(),
        color=None,
        model=None,
        effort=None,
        permission_mode="plan",
        max_turns=5,
        background=True,
        initial_prompt="Start with the failing trace.",
        memory=None,
        isolation=None,
        critical_system_reminder=None,
        omit_claude_md=False,
    )

    packet = create_worker_task_packet(
        agent,
        objective="Produce a bounded audit.",
        prompt="Inspect the tracked files and report concrete blockers.",
        scope=["Inspect the tracked files", "Summarize blockers"],
        out_of_scope=["Do not modify dependencies"],
        files_to_touch=["tracking/audit.json"],
        exit_criteria=["Write the audit summary", "List unresolved blockers"],
        evidence_expectations=["Include validation commands", "Include evidence paths"],
        ownership=TaskOwnership(owner="worker-12", writable_paths=("tracking/audit.json",), readonly_paths=("README.md",)),
    )

    runtime = SubagentRuntime(skill_registry=skill_registry, tool_registry=tool_registry)
    result = runtime.execute(
        agent,
        packet,
        lambda prepared: WorkerHandoff(
            status="partial",
            summary="Completed the audit and left one dependency unresolved.",
            completed_scope=("Inspected tracked files", "Recorded blockers"),
            requirement_disposition=("Audit written", "One blocker remains"),
            files_changed=("tracking/audit.json",),
            evidence_paths=("tracking/audit.json", "logs/audit.txt"),
            validation=(ValidationRecord(command="python3 -m pytest tests/test_aether2_agents.py -q", result="passed"),),
            unresolved_risks=("logs MCP server is still unavailable",),
            external_state=("none",),
            review=(ReviewRecord(finding="Background execution stayed disabled", disposition="noted", rationale="Runtime kept execution local and synchronous."),),
            blockers=("Need a live logs MCP registry entry before promoting this slice.",),
            recommended_next_action="Register a fake logs MCP server and rerun the bounded audit.",
        ),
    )

    prepared = result.prepared
    assert packet.permission_mode == "plan"
    assert packet.background_requested is True
    assert packet.ownership.owner == "worker-12"
    assert [item.status for item in prepared.skill_resolution] == ["resolved", "issue"]
    assert [item.status for item in prepared.mcp_resolution] == ["resolved", "retained_unresolved_inline"]
    assert set(prepared.resolved_tool_names) == {"read_file", build_mcp_tool_name("qa server", "Echo Tool")}
    assert prepared.background_execution_assumed is False

    reason_codes = {issue.reason_code for issue in prepared.issues}
    assert {
        "agent_inline_mcp_unresolved",
        "agent_tool_not_found",
        "background_execution_not_supported",
        "required_mcp_server_missing",
        "skill_not_found",
    }.issubset(reason_codes)

    parent_visible = result.parent_visible_risks()
    assert "logs MCP server is still unavailable" in parent_visible
    assert "Need a live logs MCP registry entry before promoting this slice." in parent_visible
    assert any("Background execution is not supported" in item for item in parent_visible)


def test_public_exports_include_agent_surface() -> None:
    assert public_api.SubagentRuntime is SubagentRuntime
    assert public_api.AgentDefinition is AgentDefinition
    assert public_api.WorkerHandoff is WorkerHandoff
