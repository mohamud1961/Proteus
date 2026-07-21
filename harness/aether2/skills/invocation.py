"""Visible skill-context rendering for model-facing Aether prompts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable
import json


@dataclass(frozen=True)
class RenderedSkillContext:
    text: str
    selected_skill_names: tuple[str, ...]
    truncated: bool
    total_chars: int


def render_skill_context_block(
    skills: Iterable[Any],
    *,
    max_total_chars: int = 12_000,
    max_chars_per_skill: int = 4_000,
) -> RenderedSkillContext:
    rendered_rows: list[dict[str, Any]] = []
    selected_names: list[str] = []
    truncated_any = False
    total_budget = max_total_chars

    for skill in skills:
        selected_names.append(skill.name)
        full_text = skill.rendered_text()
        content = full_text[:max_chars_per_skill]
        content_truncated = len(full_text) > len(content)
        if len(content) > total_budget:
            content = content[: max(total_budget, 0)]
            content_truncated = content_truncated or len(full_text) > len(content)
        rendered_rows.append(
            {
                "name": skill.name,
                "display_name": skill.display_name,
                "source": skill.source,
                "loaded_from": skill.loaded_from,
                "when_to_use": skill.when_to_use,
                "allowed_tools": list(skill.allowed_tools),
                "paths": list(skill.paths) if skill.paths is not None else None,
                "hooks": {
                    "present": skill.hooks.has_supported_hooks,
                    "unsupported_events": list(skill.hooks.unsupported_events),
                    "matcher_count": len(skill.hooks.matchers),
                },
                "linked_mcp_tools": list(skill.metadata.get("linked_mcp_tools", [])),
                "content_char_count": len(full_text),
                "content_sha256": sha256(full_text.encode("utf-8")).hexdigest(),
                "content_truncated": content_truncated,
                "content": content,
            }
        )
        total_budget = max_total_chars - len(json.dumps(rendered_rows, sort_keys=True, ensure_ascii=True))
        truncated_any = truncated_any or content_truncated or total_budget <= 0
        if total_budget <= 0:
            break

    payload = {
        "selected_skill_names": selected_names[: len(rendered_rows)],
        "skill_count": len(rendered_rows),
        "truncated": truncated_any,
        "skills": rendered_rows,
    }
    text = "[skills_context]\n" + json.dumps(payload, sort_keys=True, ensure_ascii=True)
    if len(text) > max_total_chars and rendered_rows:
        overflow = len(text) - max_total_chars
        last_row = rendered_rows[-1]
        last_content = str(last_row["content"])
        if overflow >= len(last_content):
            last_row["content"] = ""
        else:
            last_row["content"] = last_content[:-overflow]
        last_row["content_truncated"] = True
        payload["truncated"] = True
        text = "[skills_context]\n" + json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return RenderedSkillContext(
        text=text,
        selected_skill_names=tuple(payload["selected_skill_names"]),
        truncated=truncated_any,
        total_chars=len(text),
    )


def build_skill_prefix_message(
    skills: Iterable[Any],
    *,
    max_total_chars: int = 12_000,
    max_chars_per_skill: int = 4_000,
) -> dict[str, str]:
    rendered = render_skill_context_block(
        skills,
        max_total_chars=max_total_chars,
        max_chars_per_skill=max_chars_per_skill,
    )
    return {"role": "system", "content": rendered.text}


__all__ = ["RenderedSkillContext", "build_skill_prefix_message", "render_skill_context_block"]
