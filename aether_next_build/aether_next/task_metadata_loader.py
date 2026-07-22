"""Public task metadata loading for Terminal-Bench style task directories.

The certified runner supports both the older mirrored ``task.toml`` layout and
the current official ``task.yaml`` layout.  This module intentionally exposes
only public task facts: instructions, resource budgets, tags/categories, and
declared environment/image metadata.  Hidden grader/test material remains
outside the agent phase.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any


def load_task_metadata(task_dir: str | Path) -> dict[str, Any]:
    """Load public task metadata from ``task.toml`` or ``task.yaml``.

    Returns a normalized mapping compatible with the existing ``task_toml``
    shape used by EnvMap capability summaries.
    """
    task_path = Path(task_dir)
    toml_path = task_path / "task.toml"
    if toml_path.exists():
        try:
            return tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    for yaml_path in (task_path / "task.yaml", task_path / "task.yml"):
        if yaml_path.exists():
            try:
                return _normalize_yaml_task(_parse_simple_yaml(yaml_path.read_text(encoding="utf-8")))
            except OSError:
                return {}
    return {}


def load_task_instruction(task_dir: str | Path) -> str:
    """Return the public task instruction text.

    ``instruction.md`` is preferred for mirrored layouts.  Official YAML tasks
    store the same public prompt in ``task.yaml``.
    """
    task_path = Path(task_dir)
    instruction_path = task_path / "instruction.md"
    if instruction_path.exists():
        return instruction_path.read_text(encoding="utf-8")
    metadata = load_task_metadata(task_path)
    instruction = metadata.get("instruction")
    if isinstance(instruction, str) and instruction.strip():
        return instruction
    return "Complete the task described in /task/."


def declared_docker_image(task_dir: str | Path) -> str | None:
    """Return a public declared Docker image, if the task metadata provides one."""
    metadata = load_task_metadata(task_dir)
    candidates: list[Any] = []
    candidates.append(metadata.get("docker_image"))
    env = metadata.get("environment") if isinstance(metadata.get("environment"), dict) else {}
    candidates.append(env.get("docker_image"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_yaml_task(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "category": raw.get("category", ""),
        "difficulty": raw.get("difficulty", ""),
        "tags": raw.get("tags", ()),
        "expert_time_estimate_min": raw.get("expert_time_estimate_min"),
        "junior_time_estimate_min": raw.get("junior_time_estimate_min"),
    }
    normalized: dict[str, Any] = {
        "schema": "terminal_bench_yaml",
        "instruction": raw.get("instruction", ""),
        "metadata": metadata,
        "agent": {"timeout_sec": raw.get("max_agent_timeout_sec")},
        "verifier": {"timeout_sec": raw.get("max_test_timeout_sec")},
        "environment": {},
    }
    for key in ("docker_image", "image"):
        if isinstance(raw.get(key), str) and str(raw[key]).strip():
            normalized["environment"]["docker_image"] = str(raw[key]).strip()
            break
    return normalized


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the top-level YAML constructs used by public task manifests.

    This is deliberately small: top-level scalars, block strings, and simple
    lists.  It avoids a hard dependency on PyYAML in the certified runner while
    preserving the public metadata needed by the harness.
    """
    lines = text.splitlines()
    data: dict[str, Any] = {}
    i = 0
    key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or raw_line[:1].isspace():
            i += 1
            continue
        match = key_re.match(line)
        if not match:
            i += 1
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            base_indent: int | None = None
            block: list[str] = []
            i += 1
            while i < len(lines):
                candidate = lines[i]
                if candidate.strip() and not candidate[:1].isspace():
                    break
                if candidate.strip():
                    indent = len(candidate) - len(candidate.lstrip(" "))
                    base_indent = indent if base_indent is None else min(base_indent, indent)
                block.append(candidate)
                i += 1
            cut = base_indent or 0
            value_text = "\n".join(part[cut:] if len(part) >= cut else "" for part in block)
            data[key] = value_text.rstrip("\n")
            continue
        if value == "":
            items: list[Any] = []
            j = i + 1
            while j < len(lines):
                item_line = lines[j]
                stripped_item = item_line.strip()
                if not stripped_item or stripped_item.startswith("#"):
                    j += 1
                    continue
                if not item_line[:1].isspace():
                    break
                if stripped_item.startswith("- "):
                    items.append(_parse_scalar(stripped_item[2:].strip()))
                    j += 1
                    continue
                break
            if items:
                data[key] = items
                i = j
                continue
            data[key] = ""
            i += 1
            continue
        data[key] = _parse_scalar(value)
        i += 1
    return data


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value
