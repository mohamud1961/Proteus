"""Helpers for adaptive_profile: orientation sanitization and JSON repair."""

from __future__ import annotations

import json
import re
import time
from typing import Any

# Fields that must never reach the configurator or solver — they leak
# grader/benchmark internals.  Checked at top-level AND inside nested
# dicts such as ``env_contract``.
GRADER_LEAK_KEYS = frozenset({
    "grader_boundary",
    "hidden_tests",
    "hidden_environment_details",
    "official_grader",
    "grader_only_test_paths",
    "model_visible_test_paths",
    "hidden_tests_available_to_model",
    "benchmark",
    "leaderboard",
    "benchmark_adapter_contract",
    "contamination_policy",
    "hidden_verifier",
})

# Top-level orientation keys the solver is allowed to see.
_SOLVER_VISIBLE_KEYS = (
    "cwd", "user", "workspace_root", "writable_paths",
    "safe_file_listing", "tool_presence", "package_managers",
    "network", "runtimes", "processes", "ports",
)
_HOST_RUN_PATH_RE = re.compile(
    r"(?:"
    r"/tmp/harbor-jobs/[^\s\"'\\]+"       # Harbor job output directories
    r"|/tmp/aether2_harbor_[^\s\"'\\]+"   # Harbor snapshot temp files
    r"|(?:/home/[^\s\"'\\]+|~)/.cache/harnesseng/[^\s\"'\\]+"  # Mirror caches
    r"|(?:/home/[^\s\"'\\]+|~)/.aether2/[^\s\"'\\]+"           # Aether2 config/env files
    r")"
)


def compact_tool_catalogue(tool_schemas: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract name and description from tool schemas for compact display."""
    catalogue: list[dict[str, str]] = []
    for schema in tool_schemas:
        func = schema.get("function", schema)
        name = func.get("name", "")
        description = func.get("description", "")
        if name:
            catalogue.append({"name": name, "description": description})
    return catalogue


def parse_profile_response(raw_text: str) -> dict[str, Any] | None:
    """Extract a JSON object from the model's raw response text."""
    text = raw_text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines)
        for i in range(1, len(lines)):
            if lines[i].strip() == "```":
                end = i
                break
        text = "\n".join(lines[start:end]).strip()

    # Try direct JSON parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Try to find a JSON object in the text
    brace_start = text.find("{")
    if brace_start >= 0:
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[brace_start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
    return None


def strip_grader_keys(obj: Any) -> Any:
    """Recursively remove keys from ``GRADER_LEAK_KEYS``."""
    if isinstance(obj, dict):
        return {
            k: strip_grader_keys(v)
            for k, v in obj.items()
            if k not in GRADER_LEAK_KEYS
        }
    if isinstance(obj, list):
        return [strip_grader_keys(item) for item in obj]
    return obj


def solver_visible_orientation(orientation: dict[str, Any]) -> dict[str, Any]:
    """Extract only solver-visible fields from the full orientation snapshot.

    Two-pass filter:
    1. Allowlist: only known solver-visible top-level keys are copied.
    2. Denylist: any nested key in ``GRADER_LEAK_KEYS`` is recursively
       stripped, even if it somehow appears inside an allowed subtree.
    """
    visible: dict[str, Any] = {}
    for key in _SOLVER_VISIBLE_KEYS:
        if key in orientation:
            visible[key] = orientation[key]
    return redact_host_run_paths(strip_grader_keys(visible))


def redact_host_run_paths(obj: Any) -> Any:
    """Remove host-side run directory metadata from model-visible payloads."""
    if isinstance(obj, dict):
        return {str(key): redact_host_run_paths(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [redact_host_run_paths(item) for item in obj]
    if isinstance(obj, str):
        return _HOST_RUN_PATH_RE.sub("[host_run_path]", obj)
    return obj


_JSON_REPAIR_PROMPT = (
    "The following text was supposed to be a single valid JSON object, "
    "but it failed to parse. Return ONLY the corrected valid JSON object. "
    "Fix syntax errors (trailing commas, unescaped quotes, missing braces). "
    "Do not add commentary or markdown fences.\n\n"
)


def attempt_json_repair(
    broken_text: str,
    model_client: Any,
) -> tuple[dict[str, Any] | None, dict[str, int], float]:
    """Ask the model to fix invalid JSON. Returns (parsed_or_None, usage, duration)."""
    # Lazy import to avoid circular dependency
    from harness.aether2.runtime.adaptive_profile import parse_profile_response

    messages = [
        {"role": "system", "content": "You fix broken JSON. Output ONLY valid JSON."},
        {"role": "user", "content": _JSON_REPAIR_PROMPT + broken_text[:4000]},
    ]
    t0 = time.monotonic()
    try:
        response = model_client.call(messages, [], cache_prefix_len=0)
        raw = response.text if hasattr(response, "text") else str(response)
        usage = dict(response.usage) if hasattr(response, "usage") else {}
    except Exception:
        return None, {}, time.monotonic() - t0
    duration = time.monotonic() - t0
    parsed = parse_profile_response(raw)
    return parsed, usage, duration
