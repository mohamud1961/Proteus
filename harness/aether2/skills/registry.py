"""Owned Python skill registry and bundled-skill helpers for Aether."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
import json

from harness.aether2.tools.registry import ToolRegistry


if False:  # pragma: no cover
    from harness.aether2.skills.loader import ParsedSkillFrontmatter, SkillLoadIssue, SkillLoadResult, SkillSpec


_DEFAULT_SOURCE_PRECEDENCE = {
    "projectSettings": 0,
    "userSettings": 1,
    "policySettings": 2,
    "plugin": 3,
    "bundled": 4,
    "mcp": 5,
}


@dataclass(frozen=True)
class MCPSkillBuilders:
    create_skill_spec: Callable[..., Any]
    parse_skill_frontmatter_fields: Callable[..., Any]


_mcp_skill_builders: MCPSkillBuilders | None = None


def register_mcp_skill_builders(builders: MCPSkillBuilders) -> None:
    global _mcp_skill_builders
    _mcp_skill_builders = builders


def get_mcp_skill_builders() -> MCPSkillBuilders:
    if _mcp_skill_builders is None:
        raise RuntimeError(
            "MCP skill builders not registered — harness.aether2.skills.loader has not been evaluated yet"
        )
    return _mcp_skill_builders


@dataclass(frozen=True)
class BundledSkillDefinition:
    name: str
    description: str
    prompt: str
    aliases: tuple[str, ...] = ()
    when_to_use: str | None = None
    argument_hint: str | None = None
    allowed_tools: tuple[str, ...] = ()
    model: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    hooks: Any = None
    execution_context: str | None = None
    agent: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillRegistryIssue:
    reason_code: str
    message: str
    skill_name: str | None = None
    file_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "skill_name": self.skill_name,
            "file_path": self.file_path,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class LinkedMcpTool:
    qualified_name: str
    server_name: str | None
    original_name: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "qualified_name": self.qualified_name,
            "server_name": self.server_name,
            "original_name": self.original_name,
        }


@dataclass(frozen=True)
class SkillSelectionResult:
    skills: tuple[Any, ...] = ()
    issues: tuple[SkillRegistryIssue, ...] = ()


_bundled_skill_definitions: list[BundledSkillDefinition] = []


def register_bundled_skill(definition: BundledSkillDefinition) -> None:
    _bundled_skill_definitions.append(definition)


def get_bundled_skill_definitions() -> tuple[BundledSkillDefinition, ...]:
    return tuple(_bundled_skill_definitions)


def clear_bundled_skills() -> None:
    _bundled_skill_definitions.clear()


def materialize_bundled_skill(
    definition: BundledSkillDefinition,
    *,
    extract_root: str | Path | None = None,
) -> Any:
    from harness.aether2.skills.loader import SkillHookMetadata, create_skill_spec

    skill_root: str | None = None
    if definition.files:
        if extract_root is None:
            raise ValueError("extract_root is required when materializing bundled skill files")
        skill_root = str(extract_bundled_skill_files(definition.name, definition.files, extract_root))
    hooks = definition.hooks if isinstance(definition.hooks, SkillHookMetadata) else SkillHookMetadata()
    return create_skill_spec(
        skill_name=definition.name,
        display_name=None,
        description=definition.description,
        has_user_specified_description=True,
        markdown_content=definition.prompt,
        allowed_tools=definition.allowed_tools,
        argument_hint=definition.argument_hint,
        argument_names=(),
        when_to_use=definition.when_to_use,
        version=None,
        model=definition.model,
        disable_model_invocation=definition.disable_model_invocation,
        user_invocable=definition.user_invocable,
        source="bundled",
        base_dir=skill_root,
        loaded_from="bundled",
        hooks=hooks,
        execution_context=definition.execution_context,
        agent=definition.agent,
        paths=None,
        effort=None,
        shell=None,
        file_path=None,
        metadata=definition.metadata,
    )


def extract_bundled_skill_files(
    skill_name: str,
    files: dict[str, str],
    extract_root: str | Path,
) -> Path:
    target_root = Path(extract_root) / skill_name
    for rel_path, content in files.items():
        destination = _resolve_skill_file_path(target_root, rel_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return target_root


class SkillRegistry:
    """Deterministic registry for repo-local, bundled, and future MCP-backed skills."""

    def __init__(self, *, tool_registry: ToolRegistry | None = None) -> None:
        self._tool_registry = tool_registry
        self._skills: dict[str, Any] = {}
        self._name_rank: dict[str, tuple[int, int]] = {}
        self._canonical_paths: dict[str, str] = {}
        self._issues: list[SkillRegistryIssue] = []
        self._load_issues: list[Any] = []
        self._order = 0

    def register_load_result(
        self,
        load_result: Any,
        *,
        source_precedence: int | None = None,
    ) -> "SkillRegistry":
        for issue in getattr(load_result, "issues", ()):
            self._load_issues.append(issue)
        for skill in getattr(load_result, "skills", ()):
            self.register_skill(skill, source_precedence=source_precedence)
        return self

    def register_skill(
        self,
        skill: Any,
        *,
        source_precedence: int | None = None,
    ) -> Any:
        precedence = _DEFAULT_SOURCE_PRECEDENCE.get(skill.source, 50) if source_precedence is None else source_precedence
        self._order += 1
        if skill.canonical_file_path:
            winner = self._canonical_paths.get(skill.canonical_file_path)
            if winner is not None:
                self._issues.append(
                    SkillRegistryIssue(
                        reason_code="duplicate_skill_realpath",
                        message=f"Skipping skill '{skill.name}' because the same file is already registered as '{winner}'",
                        skill_name=skill.name,
                        file_path=skill.file_path,
                        metadata={"canonical_file_path": skill.canonical_file_path, "winner": winner},
                    )
                )
                return self._skills[winner]

        existing = self._skills.get(skill.name)
        existing_rank = self._name_rank.get(skill.name)
        if existing is not None and existing_rank is not None:
            existing_precedence, existing_order = existing_rank
            keep_existing = (existing_precedence, existing_order) <= (precedence, self._order)
            if keep_existing:
                self._issues.append(
                    SkillRegistryIssue(
                        reason_code="skill_name_collision",
                        message=f"Skipping skill '{skill.name}' because the name is already owned by '{existing.source}'",
                        skill_name=skill.name,
                        file_path=skill.file_path,
                        metadata={
                            "winner_source": existing.source,
                            "winner_file_path": existing.file_path,
                            "loser_source": skill.source,
                        },
                    )
                )
                return existing
            self._issues.append(
                SkillRegistryIssue(
                    reason_code="skill_name_collision",
                    message=f"Replacing skill '{skill.name}' with a higher-priority registration",
                    skill_name=skill.name,
                    file_path=skill.file_path,
                    metadata={
                        "replaced_source": existing.source,
                        "replaced_file_path": existing.file_path,
                        "winner_source": skill.source,
                    },
                )
            )

        linked_tools = self._linked_mcp_tools(skill)
        if linked_tools:
            metadata = dict(skill.metadata)
            metadata["linked_mcp_tools"] = [tool.as_dict() for tool in linked_tools]
            skill = type(skill)(**{**skill.__dict__, "metadata": metadata})
        self._skills[skill.name] = skill
        self._name_rank[skill.name] = (precedence, self._order)
        if skill.canonical_file_path:
            self._canonical_paths[skill.canonical_file_path] = skill.name
        return skill

    def register_bundled_defaults(self, *, extract_root: str | Path | None = None) -> "SkillRegistry":
        for definition in get_bundled_skill_definitions():
            self.register_skill(materialize_bundled_skill(definition, extract_root=extract_root))
        return self

    def issues(self) -> list[SkillRegistryIssue]:
        issues = [issue if isinstance(issue, SkillRegistryIssue) else SkillRegistryIssue(**issue.as_dict()) for issue in self._issues]
        for issue in self._load_issues:
            issues.append(
                SkillRegistryIssue(
                    reason_code=issue.reason_code,
                    message=issue.message,
                    skill_name=issue.skill_name,
                    file_path=issue.file_path,
                    metadata=dict(issue.metadata),
                )
            )
        return issues

    def get(self, skill_name: str) -> Any | None:
        return self._skills.get(skill_name)

    def all(self) -> list[Any]:
        return list(self._skills.values())

    def names(self) -> list[str]:
        return list(self._skills)

    def select(
        self,
        skill_refs: Iterable[str],
        *,
        file_paths: Iterable[str | Path] | None = None,
        cwd: str | Path | None = None,
    ) -> SkillSelectionResult:
        from harness.aether2.skills.loader import skill_matches_paths

        issues: list[SkillRegistryIssue] = []
        selected: list[Any] = []
        seen_names: set[str] = set()
        for ref in skill_refs:
            skill = self._skills.get(ref)
            if skill is None:
                issues.append(
                    SkillRegistryIssue(
                        reason_code="skill_not_found",
                        message=f"Skill reference '{ref}' was not found in the registry",
                        skill_name=ref,
                    )
                )
                continue
            if ref in seen_names:
                issues.append(
                    SkillRegistryIssue(
                        reason_code="duplicate_skill_ref",
                        message=f"Skill reference '{ref}' was provided more than once",
                        skill_name=ref,
                    )
                )
                continue
            if file_paths is not None and cwd is not None and not skill_matches_paths(skill, file_paths, cwd):
                issues.append(
                    SkillRegistryIssue(
                        reason_code="skill_path_scope_mismatch",
                        message=f"Skill '{ref}' did not match the provided file paths",
                        skill_name=ref,
                        metadata={"paths": list(skill.paths or ())},
                    )
                )
                continue
            selected.append(skill)
            seen_names.add(ref)
        return SkillSelectionResult(skills=tuple(selected), issues=tuple(issues))

    def audit(self) -> dict[str, Any]:
        return {
            "skills": [skill.as_dict() for skill in self.all()],
            "issues": [issue.as_dict() for issue in self.issues()],
        }

    def _linked_mcp_tools(self, skill: Any) -> tuple[LinkedMcpTool, ...]:
        if self._tool_registry is None:
            return ()
        linked: list[LinkedMcpTool] = []
        for tool_name in skill.allowed_tools:
            registration = self._tool_registry.get(tool_name)
            if registration is None or registration.kind != "mcp":
                continue
            linked.append(
                LinkedMcpTool(
                    qualified_name=registration.name,
                    server_name=registration.server_name,
                    original_name=registration.original_name,
                )
            )
        return tuple(linked)


def _resolve_skill_file_path(base_dir: Path, rel_path: str) -> Path:
    candidate = (base_dir / rel_path).resolve(strict=False)
    try:
        candidate.relative_to(base_dir.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"bundled skill file path escapes skill dir: {rel_path}") from exc
    return candidate


__all__ = [
    "BundledSkillDefinition",
    "LinkedMcpTool",
    "MCPSkillBuilders",
    "SkillRegistry",
    "SkillRegistryIssue",
    "SkillSelectionResult",
    "clear_bundled_skills",
    "extract_bundled_skill_files",
    "get_bundled_skill_definitions",
    "get_mcp_skill_builders",
    "materialize_bundled_skill",
    "register_bundled_skill",
    "register_mcp_skill_builders",
]
