"""YAML frontmatter parsing and field coercion helpers for skills/loader.py.

Extracted from loader.py to keep that module under 500 LOC.
These are internal helpers; callers should import from skills/loader.py.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Iterable, Mapping

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}
_FRONTMATTER_BOUNDARY = "---"


# ---------------------------------------------------------------------------
# YAML micro-parser (handles the subset used in skill frontmatter)
# ---------------------------------------------------------------------------

def _parse_simple_yaml(raw_frontmatter: str) -> dict[str, Any]:
    lines = raw_frontmatter.splitlines()
    payload, index = _parse_yaml_block(lines, 0, 0)
    if index < len(lines):
        trailing = next((line for line in lines[index:] if line.strip()), None)
        if trailing is not None:
            raise ValueError(f"unexpected trailing content: {trailing}")
    if not isinstance(payload, dict):
        raise ValueError("frontmatter root must be a mapping")
    return payload


def _parse_yaml_block(lines: list[str], start: int, indent: int) -> tuple[Any, int]:
    mapping: dict[str, Any] = {}
    sequence: list[Any] = []
    mode: str | None = None
    index = start
    while index < len(lines):
        raw_line = lines[index]
        if not raw_line.strip():
            index += 1
            continue
        current_indent = len(raw_line) - len(raw_line.lstrip(" "))
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"unexpected indentation at line: {raw_line.strip()}")
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            if mode not in (None, "list"):
                raise ValueError("cannot mix mapping and list items at the same indentation level")
            mode = "list"
            item_text = stripped[2:].strip()
            index += 1
            item, index = _parse_list_item(item_text, lines, index, indent + 2)
            sequence.append(item)
            continue
        if mode not in (None, "map"):
            raise ValueError("cannot mix list and mapping items at the same indentation level")
        mode = "map"
        key, inline_value = _split_key_value(stripped)
        if key is None:
            raise ValueError(f"invalid mapping entry: {stripped}")
        index += 1
        if inline_value is None:
            nested, index = _parse_yaml_block(lines, index, indent + 2)
            mapping[key] = nested
        else:
            mapping[key] = _parse_scalar(inline_value)
    if mode == "list":
        return sequence, index
    return mapping, index


def _parse_list_item(item_text: str, lines: list[str], index: int, indent: int) -> tuple[Any, int]:
    if not item_text:
        return _parse_yaml_block(lines, index, indent)
    key, inline_value = _split_key_value(item_text)
    if key is None:
        return _parse_scalar(item_text), index
    payload: dict[str, Any] = {}
    payload[key] = {} if inline_value is None else _parse_scalar(inline_value)
    while index < len(lines):
        raw_line = lines[index]
        if not raw_line.strip():
            index += 1
            continue
        current_indent = len(raw_line) - len(raw_line.lstrip(" "))
        if current_indent < indent:
            break
        if current_indent > indent:
            if inline_value is not None:
                nested, index = _parse_yaml_block(lines, index, indent)
                if not isinstance(nested, dict):
                    raise ValueError("list-item continuation must be a mapping")
                payload.update(nested)
                continue
            nested, index = _parse_yaml_block(lines, index, indent + 2)
            payload[key] = nested
            continue
        next_key, next_inline = _split_key_value(raw_line.strip())
        if next_key is None:
            break
        index += 1
        if next_inline is None:
            nested, index = _parse_yaml_block(lines, index, indent + 2)
            payload[next_key] = nested
        else:
            payload[next_key] = _parse_scalar(next_inline)
    return payload, index


def _split_key_value(text: str) -> tuple[str | None, str | None]:
    if ":" not in text:
        return None, None
    key, remainder = text.split(":", 1)
    key = key.strip()
    if not key:
        return None, None
    remainder = remainder.lstrip()
    if not remainder:
        return key, None
    return key, remainder


def _parse_scalar(value: str) -> Any:
    if value in {"''", '""'}:
        return ""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if (value.startswith("[") and not value.endswith("]")) or (value.startswith("{") and not value.endswith("}")):
        raise ValueError(f"malformed scalar value: {value}")
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"malformed quoted scalar: {value}") from exc
    return value


# ---------------------------------------------------------------------------
# Field coercion helpers
# ---------------------------------------------------------------------------

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


def _parse_argument_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        tokens = re.split(r"[\s,]+", value.strip())
        return [token for token in tokens if token]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    items.append(stripped)
        return items
    return []


def _parse_skill_paths(value: Any) -> tuple[str, ...] | None:
    patterns = _parse_string_list(value, delimiter=",")
    normalized = []
    for pattern in patterns:
        candidate = pattern.strip()
        if candidate.endswith("/**"):
            candidate = candidate[:-3]
        if candidate:
            normalized.append(candidate)
    if not normalized or all(pattern == "**" for pattern in normalized):
        return None
    return tuple(normalized)


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _coerce_model(value: Any) -> str | None:
    model = _coerce_optional_string(value)
    if model == "inherit":
        return None
    return model


def _extract_description_from_markdown(markdown: str, fallback_label: str) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if line.startswith("#"):
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    if paragraphs:
        return paragraphs[0]
    return f"{fallback_label} {fallback_label.lower()} content."
