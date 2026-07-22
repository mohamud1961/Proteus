from __future__ import annotations

import json
from pathlib import Path

import harness.aether2 as public_api
from harness.aether2.runtime.context import ContextManager
from harness.aether2.skills import (
    BundledSkillDefinition,
    SkillRegistry,
    build_skill_prefix_message,
    clear_bundled_skills,
    create_skill_spec,
    discover_skill_directories_for_paths,
    get_mcp_skill_builders,
    load_skills_from_directory,
    materialize_bundled_skill,
    parse_frontmatter_document,
    render_skill_context_block,
)
from harness.aether2.tools import FakeLocalMcpServer, McpServerConfig, McpToolDescriptor, McpToolResult, build_mcp_tool_name, build_native_tool_registry, connect_fake_local_server


def _write_skill(path: Path, text: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")


def _make_tool_registry():
    server = FakeLocalMcpServer(
        tools=[
            McpToolDescriptor(
                name="Echo Tool",
                description="Return the payload.",
                input_json_schema={
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                    "required": ["payload"],
                    "additionalProperties": False,
                },
            )
        ],
        handlers={
            "Echo Tool": lambda arguments, timeout_sec=None: McpToolResult(content=arguments["payload"]),
        },
    )
    connection = connect_fake_local_server(
        "qa server",
        server,
        config=McpServerConfig(type="fake_local", timeout_sec=5),
    )
    return build_native_tool_registry().register_mcp_connection(connection)


def test_load_skills_from_directory_parses_frontmatter_and_discovers_in_sorted_order(tmp_path: Path) -> None:
    skills_root = tmp_path / ".claude" / "skills"
    _write_skill(
        skills_root / "alpha-skill",
        """---
description: Alpha description
allowed-tools:
  - read_file
  - mcp__qa_server__Echo_Tool
argument-hint: "<target>"
arguments:
  - target
when_to_use: Use alpha when deterministic skill loading matters.
version: 1.2.3
user-invocable: false
disable-model-invocation: true
context: fork
agent: auditor
paths:
  - src/**
  - docs/*.md
hooks:
  PreToolUse:
    - matcher: read_file|write_file
      hooks:
        - command: python3 hooks/audit.py
          once: true
  Stop:
    - matcher: ""
      hooks:
        - command: python3 hooks/ignored.py
---
# Alpha

Alpha body.
""",
    )
    _write_skill(
        skills_root / "beta-skill",
        """# Beta

Fallback description paragraph.
""",
    )
    (skills_root / "notes").mkdir(parents=True)

    result = load_skills_from_directory(skills_root, "projectSettings")

    assert [skill.name for skill in result.skills] == ["alpha-skill", "beta-skill"]
    alpha = result.skills[0]
    beta = result.skills[1]
    assert alpha.description == "Alpha description"
    assert alpha.allowed_tools == ("read_file", "mcp__qa_server__Echo_Tool")
    assert alpha.argument_names == ("target",)
    assert alpha.argument_hint == "<target>"
    assert alpha.when_to_use == "Use alpha when deterministic skill loading matters."
    assert alpha.version == "1.2.3"
    assert alpha.user_invocable is False
    assert alpha.disable_model_invocation is True
    assert alpha.execution_context == "fork"
    assert alpha.agent == "auditor"
    assert alpha.paths == ("src", "docs/*.md")
    assert alpha.hooks.has_supported_hooks is True
    assert alpha.hooks.matchers[0].normalized_event == "pre_tool_use"
    assert alpha.hooks.matchers[0].hooks[0].once is True
    assert alpha.hooks.unsupported_events == ("Stop",)
    assert beta.description == "Fallback description paragraph."
    assert any(issue.reason_code == "skill_file_missing" for issue in result.issues)
    assert any(issue.reason_code == "unsupported_skill_hook_event" for issue in result.issues)


def test_discover_skill_directories_and_path_matching_are_deterministic(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    deep_dir = repo_root / "services" / "api" / ".claude" / "skills"
    shallow_dir = repo_root / "services" / ".claude" / "skills"
    deep_dir.mkdir(parents=True)
    shallow_dir.mkdir(parents=True)
    target_file = repo_root / "services" / "api" / "src" / "main.py"
    target_file.parent.mkdir(parents=True)
    target_file.write_text("print('hi')\n", encoding="utf-8")

    discovered = discover_skill_directories_for_paths([target_file], repo_root)

    assert discovered == (str(deep_dir.resolve()), str(shallow_dir.resolve()))


def test_skill_registry_handles_realpath_duplicates_and_name_collisions_deterministically(tmp_path: Path) -> None:
    skills_root = tmp_path / ".claude" / "skills"
    _write_skill(skills_root / "alpha-skill", "Alpha")
    alias_root = skills_root / "z-alias-skill"
    alias_root.symlink_to(skills_root / "alpha-skill", target_is_directory=True)
    result = load_skills_from_directory(skills_root, "projectSettings")

    registry = SkillRegistry()
    registry.register_load_result(result, source_precedence=0)
    assert registry.get("alpha-skill") is not None
    assert registry.get("z-alias-skill") is None
    assert any(issue.reason_code == "duplicate_skill_realpath" for issue in registry.issues())

    existing = registry.get("alpha-skill")
    assert existing is not None
    colliding = create_skill_spec(
        skill_name="alpha-skill",
        display_name=None,
        description="Bundled override",
        has_user_specified_description=True,
        markdown_content="bundled",
        allowed_tools=(),
        argument_hint=None,
        argument_names=(),
        when_to_use=None,
        version=None,
        model=None,
        disable_model_invocation=False,
        user_invocable=True,
        source="bundled",
        base_dir=None,
        loaded_from="bundled",
        hooks=existing.hooks,
        execution_context=None,
        agent=None,
        paths=None,
        effort=None,
        shell=None,
    )
    registry.register_skill(colliding, source_precedence=10)

    kept = registry.get("alpha-skill")
    assert kept is not None and kept.source == "projectSettings"
    assert any(issue.reason_code == "skill_name_collision" for issue in registry.issues())


def test_skill_selection_and_visible_context_block_are_explicit_and_bounded(tmp_path: Path) -> None:
    skill = create_skill_spec(
        skill_name="alpha-skill",
        display_name="Alpha Skill",
        description="Selected skill",
        has_user_specified_description=True,
        markdown_content="A" * 6000,
        allowed_tools=("read_file",),
        argument_hint=None,
        argument_names=(),
        when_to_use="Use when explicit skills are requested.",
        version=None,
        model=None,
        disable_model_invocation=False,
        user_invocable=True,
        source="projectSettings",
        base_dir=str(tmp_path / "alpha-skill"),
        loaded_from="skills",
        hooks=public_api.SkillHookMetadata(),
        execution_context=None,
        agent=None,
        paths=None,
        effort=None,
        shell=None,
        metadata={"hook_placeholder": True},
    )
    registry = SkillRegistry()
    registry.register_skill(skill, source_precedence=0)

    selection = registry.select(["alpha-skill", "missing-skill", "alpha-skill"])
    assert [item.name for item in selection.skills] == ["alpha-skill"]
    assert [issue.reason_code for issue in selection.issues] == ["skill_not_found", "duplicate_skill_ref"]

    rendered = render_skill_context_block(selection.skills, max_total_chars=3000, max_chars_per_skill=1200)
    assert rendered.truncated is True
    assert rendered.selected_skill_names == ("alpha-skill",)
    assert rendered.total_chars <= 3000
    assert "[skills_context]" in rendered.text
    assert "Base directory for this skill:" in rendered.text

    context = ContextManager()
    context.build_prefix(
        system_prompt="system",
        task_instruction="task",
        orientation={"cwd": str(tmp_path)},
        tool_schemas=[],
        extra_prefix_messages=[build_skill_prefix_message(selection.skills, max_total_chars=3000, max_chars_per_skill=1200)],
    )
    assert any("[skills_context]" in message["content"] for message in context.message_history())
    context.assert_prefix_unchanged()


def test_skill_registry_links_mcp_metadata_and_does_not_mutate_context_implicitly(tmp_path: Path) -> None:
    tool_registry = _make_tool_registry()
    skill = create_skill_spec(
        skill_name="tool-linked",
        display_name=None,
        description="Tool-linked skill",
        has_user_specified_description=True,
        markdown_content="Use the linked MCP tool when explicitly selected.",
        allowed_tools=(build_mcp_tool_name("qa server", "Echo Tool"),),
        argument_hint=None,
        argument_names=(),
        when_to_use="Use only when the MCP tool is explicitly requested.",
        version=None,
        model=None,
        disable_model_invocation=False,
        user_invocable=True,
        source="projectSettings",
        base_dir=str(tmp_path / "tool-linked"),
        loaded_from="skills",
        hooks=public_api.SkillHookMetadata(),
        execution_context=None,
        agent=None,
        paths=("src",),
        effort=None,
        shell=None,
    )
    registry = SkillRegistry(tool_registry=tool_registry)
    registry.register_skill(skill, source_precedence=0)

    linked = registry.get("tool-linked")
    assert linked is not None
    assert linked.metadata["linked_mcp_tools"] == [
        {
            "original_name": "Echo Tool",
            "qualified_name": build_mcp_tool_name("qa server", "Echo Tool"),
            "server_name": "qa server",
        }
    ]

    context = ContextManager()
    context.build_prefix(
        system_prompt="system",
        task_instruction="task",
        orientation={"cwd": str(tmp_path)},
        tool_schemas=[],
    )
    assert all("[skills_context]" not in message["content"] for message in context.message_history())

    mismatch = registry.select(["tool-linked"], file_paths=[tmp_path / "docs" / "notes.md"], cwd=tmp_path)
    assert mismatch.skills == ()
    assert mismatch.issues[0].reason_code == "skill_path_scope_mismatch"


def test_bundled_skill_extraction_mcp_builder_registry_and_public_exports(tmp_path: Path) -> None:
    clear_bundled_skills()
    definition = BundledSkillDefinition(
        name="verify-lite",
        description="Verify changes.",
        prompt="Run the app and confirm the requested behavior.",
        files={"notes/checklist.md": "check the app"},
    )
    skill = materialize_bundled_skill(definition, extract_root=tmp_path)

    assert (tmp_path / "verify-lite" / "notes" / "checklist.md").read_text(encoding="utf-8") == "check the app"
    assert skill.skill_root == str((tmp_path / "verify-lite").resolve())
    assert public_api.SkillRegistry is SkillRegistry
    assert public_api.load_skills_from_directory is load_skills_from_directory
    assert callable(get_mcp_skill_builders().create_skill_spec)


def test_parse_frontmatter_document_reports_invalid_yaml() -> None:
    frontmatter, body, issues = parse_frontmatter_document(
        "---\ndescription: [unterminated\n---\nbody\n",
        "broken/SKILL.md",
    )

    assert frontmatter == {}
    assert body == "body"
    assert issues[0].reason_code == "invalid_frontmatter"
