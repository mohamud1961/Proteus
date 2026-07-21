"""Public task resource metadata exposed without benchmark semantics."""
from __future__ import annotations

from typing import Any, Mapping


def flatten_task_toml(task_toml: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return internal metadata plus a model-safe resource budget.

    Category, difficulty, tags, task names, and image identity remain internal.
    Only concrete runtime/resource limits are eligible for model-facing EnvMap
    material.
    """
    data = dict(task_toml or {})
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    agent = data.get("agent") if isinstance(data.get("agent"), Mapping) else {}
    verifier = data.get("verifier") if isinstance(data.get("verifier"), Mapping) else {}
    environment = data.get("environment") if isinstance(data.get("environment"), Mapping) else {}
    tags = tuple(str(tag) for tag in metadata.get("tags", ()) if str(tag).strip())
    return {
        "task_version": data.get("version", ""),
        "category": str(metadata.get("category", "")),
        "difficulty": str(metadata.get("difficulty", "")),
        "tags": tags,
        "expert_time_estimate_min": metadata.get("expert_time_estimate_min"),
        "junior_time_estimate_min": metadata.get("junior_time_estimate_min"),
        "agent_timeout_sec": agent.get("timeout_sec"),
        "verifier_timeout_sec": verifier.get("timeout_sec"),
        "environment": dict(environment),
        "resource_budget": {
            "agent_timeout_sec": agent.get("timeout_sec"),
            "verifier_timeout_sec": verifier.get("timeout_sec"),
            "build_timeout_sec": environment.get("build_timeout_sec"),
            "cpus": environment.get("cpus"),
            "memory": environment.get("memory"),
            "storage": environment.get("storage"),
            "docker_image": environment.get("docker_image"),
        },
    }
