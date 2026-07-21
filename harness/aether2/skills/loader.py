"""Owned Python skill loader for the Aether runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping
import json

from harness.aether2.skills.registry import MCPSkillBuilders, register_mcp_skill_builders
from harness.aether2.skills.skill_types import (
    LoadedFrom,
    ParsedSkillFrontmatter,
    SkillExecutionContext,
    SkillHookCommand,
    SkillHookEvent,
    SkillHookMatcher,
    SkillHookMetadata,
    SkillLoadIssue,
    SkillSource,
    parse_frontmatter_document,
    parse_skill_frontmatter_fields,
)

# Re-export public types for backward compatibility
__all__ = [
    "LoadedFrom",
    "ParsedSkillFrontmatter",
    "SkillExecutionContext",
    "SkillHookCommand",
    "SkillHookEvent",
    "SkillHookMatcher",
    "SkillHookMetadata",
    "SkillLoadIssue",
    "SkillLoadResult",
    "SkillSource",
    "SkillSpec",
    "create_skill_spec",
    "discover_skill_directories_for_paths",
    "estimate_skill_frontmatter_tokens",
    "load_skills_from_directory",
    "parse_frontmatter_document",
    "parse_skill_frontmatter_fields",
    "skill_matches_paths",
]


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    content: str
    source: SkillSource
    loaded_from: LoadedFrom
    skill_root: str | None = None
    file_path: str | None = None
    canonical_file_path: str | None = None
    display_name: str | None = None
    when_to_use: str | None = None
    version: str | None = None
    model: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    allowed_tools: tuple[str, ...] = ()
    argument_hint: str | None = None
    argument_names: tuple[str, ...] = ()
    hooks: SkillHookMetadata = field(default_factory=SkillHookMetadata)
    execution_context: SkillExecutionContext | None = None
    agent: str | None = None
    effort: str | int | None = None
    shell: str | dict[str, Any] | None = None
    paths: tuple[str, ...] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_hidden(self) -> bool:
        return not self.user_invocable

    @property
    def content_length(self) -> int:
        return len(self.content)

    def user_facing_name(self) -> str:
        return self.display_name or self.name

    def rendered_text(self) -> str:
        if not self.skill_root:
            return self.content
        return f"Base directory for this skill: {self.skill_root}\n\n{self.content}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "source": self.source,
            "loaded_from": self.loaded_from,
            "skill_root": self.skill_root,
            "file_path": self.file_path,
            "canonical_file_path": self.canonical_file_path,
            "when_to_use": self.when_to_use,
            "version": self.version,
            "model": self.model,
            "disable_model_invocation": self.disable_model_invocation,
            "user_invocable": self.user_invocable,
            "allowed_tools": list(self.allowed_tools),
            "argument_hint": self.argument_hint,
            "argument_names": list(self.argument_names),
            "hooks": self.hooks.as_dict(),
            "execution_context": self.execution_context,
            "agent": self.agent,
            "effort": self.effort,
            "shell": self.shell,
            "paths": list(self.paths) if self.paths is not None else None,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }


@dataclass(frozen=True)
class SkillLoadResult:
    skills: tuple[SkillSpec, ...] = ()
    issues: tuple[SkillLoadIssue, ...] = ()


def estimate_skill_frontmatter_tokens(skill: SkillSpec) -> int:
    frontmatter_text = " ".join(
        part for part in (skill.name, skill.description, skill.when_to_use or "") if part
    )
    return max(1, len(frontmatter_text) // 4) if frontmatter_text else 0


def create_skill_spec(
    *,
    skill_name: str,
    display_name: str | None,
    description: str,
    has_user_specified_description: bool,
    markdown_content: str,
    allowed_tools: Iterable[str],
    argument_hint: str | None,
    argument_names: Iterable[str],
    when_to_use: str | None,
    version: str | None,
    model: str | None,
    disable_model_invocation: bool,
    user_invocable: bool,
    source: SkillSource,
    base_dir: str | None,
    loaded_from: LoadedFrom,
    hooks: SkillHookMetadata,
    execution_context: SkillExecutionContext | None,
    agent: str | None,
    paths: tuple[str, ...] | None,
    effort: str | int | None,
    shell: str | dict[str, Any] | None,
    file_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SkillSpec:
    canonical_path: str | None = None
    if file_path is not None:
        try:
            canonical_path = str(Path(file_path).resolve(strict=True))
        except FileNotFoundError:
            canonical_path = None
    base_metadata = {
        "has_user_specified_description": has_user_specified_description,
        "content_length": len(markdown_content),
    }
    if metadata:
        base_metadata.update(json.loads(json.dumps(dict(metadata), sort_keys=True, ensure_ascii=True)))
    return SkillSpec(
        name=skill_name,
        display_name=display_name,
        description=description,
        content=markdown_content,
        source=source,
        loaded_from=loaded_from,
        skill_root=base_dir,
        file_path=file_path,
        canonical_file_path=canonical_path,
        when_to_use=when_to_use,
        version=version,
        model=model,
        disable_model_invocation=disable_model_invocation,
        user_invocable=user_invocable,
        allowed_tools=tuple(allowed_tools),
        argument_hint=argument_hint,
        argument_names=tuple(argument_names),
        hooks=hooks,
        execution_context=execution_context,
        agent=agent,
        effort=effort,
        shell=shell,
        paths=paths,
        metadata=base_metadata,
    )


def load_skills_from_directory(base_path: str | Path, source: SkillSource) -> SkillLoadResult:
    base_dir = Path(base_path)
    if not base_dir.exists():
        return SkillLoadResult(
            issues=(
                SkillLoadIssue(
                    reason_code="skills_directory_missing",
                    message="skills directory does not exist",
                    file_path=str(base_dir),
                    source=source,
                ),
            )
        )
    if not base_dir.is_dir():
        return SkillLoadResult(
            issues=(
                SkillLoadIssue(
                    reason_code="skills_directory_invalid",
                    message="skills base path is not a directory",
                    file_path=str(base_dir),
                    source=source,
                ),
            )
        )

    skills: list[SkillSpec] = []
    issues: list[SkillLoadIssue] = []
    for entry in sorted(base_dir.iterdir(), key=lambda item: item.name):
        if not (entry.is_dir() or entry.is_symlink()):
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            issues.append(
                SkillLoadIssue(
                    reason_code="skill_file_missing",
                    message="Skill directory does not contain SKILL.md",
                    skill_name=entry.name,
                    file_path=str(skill_file),
                    source=source,
                )
            )
            continue
        try:
            raw_text = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(
                SkillLoadIssue(
                    reason_code="skill_read_failed",
                    message=f"Failed to read skill file: {exc}",
                    skill_name=entry.name,
                    file_path=str(skill_file),
                    source=source,
                )
            )
            continue
        frontmatter, content, parse_issues = parse_frontmatter_document(raw_text, str(skill_file))
        issues.extend(parse_issues)
        parsed = parse_skill_frontmatter_fields(frontmatter, content, entry.name)
        issues.extend(parsed.issues)
        skills.append(
            create_skill_spec(
                skill_name=entry.name,
                display_name=parsed.display_name,
                description=parsed.description,
                has_user_specified_description=parsed.has_user_specified_description,
                markdown_content=content,
                allowed_tools=parsed.allowed_tools,
                argument_hint=parsed.argument_hint,
                argument_names=parsed.argument_names,
                when_to_use=parsed.when_to_use,
                version=parsed.version,
                model=parsed.model,
                disable_model_invocation=parsed.disable_model_invocation,
                user_invocable=parsed.user_invocable,
                source=source,
                base_dir=str(entry.resolve()),
                loaded_from="skills",
                hooks=parsed.hooks,
                execution_context=parsed.execution_context,
                agent=parsed.agent,
                paths=parsed.paths,
                effort=parsed.effort,
                shell=parsed.shell,
                file_path=str(skill_file),
            )
        )
    return SkillLoadResult(skills=tuple(skills), issues=tuple(issues))


def discover_skill_directories_for_paths(
    file_paths: Iterable[str | Path],
    cwd: str | Path,
    *,
    seen_dirs: Iterable[str | Path] | None = None,
) -> tuple[str, ...]:
    resolved_cwd = Path(cwd).resolve()
    seen = {str(Path(path).resolve()) for path in (seen_dirs or ())}
    discovered: set[str] = set()
    for file_path in file_paths:
        current_dir = Path(file_path).resolve().parent
        while current_dir != resolved_cwd and _is_within(current_dir, resolved_cwd):
            skill_dir = current_dir / ".claude" / "skills"
            skill_dir_key = str(skill_dir.resolve(strict=False))
            if skill_dir_key not in seen and skill_dir.is_dir():
                discovered.add(skill_dir_key)
                seen.add(skill_dir_key)
            parent = current_dir.parent
            if parent == current_dir:
                break
            current_dir = parent
    return tuple(sorted(discovered, key=lambda path: (-len(Path(path).parts), path)))


def skill_matches_paths(skill: SkillSpec, file_paths: Iterable[str | Path], cwd: str | Path) -> bool:
    if not skill.paths:
        return True
    resolved_cwd = Path(cwd).resolve()
    patterns = tuple(skill.paths)
    for file_path in file_paths:
        relative_path = _relative_posix_path(file_path, resolved_cwd)
        if relative_path is None:
            continue
        if any(_matches_pattern(relative_path, pattern) for pattern in patterns):
            return True
    return False


def _relative_posix_path(file_path: str | Path, resolved_cwd: Path) -> str | None:
    candidate = Path(file_path).resolve()
    try:
        relative = candidate.relative_to(resolved_cwd)
    except ValueError:
        return None
    value = relative.as_posix()
    return value or None


def _matches_pattern(relative_path: str, pattern: str) -> bool:
    try:
        return Path(relative_path).match(pattern)
    except ValueError:
        return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


register_mcp_skill_builders(
    MCPSkillBuilders(
        create_skill_spec=create_skill_spec,
        parse_skill_frontmatter_fields=parse_skill_frontmatter_fields,
    )
)
