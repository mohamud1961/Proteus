"""Helpers for detecting Azure OpenAI routes and OpenAI-compatible env mappings."""

from __future__ import annotations

import os
from typing import Any, Mapping

from harness.aether2.runtime.model_routes import (
    AZURE_ENV_API_VERSION,
    AZURE_ENV_ENDPOINT,
    AZURE_ENV_GPT53_CODEX_DEPLOYMENT,
    AZURE_ENV_GPT53_CODEX_KEY,
    AZURE_ENV_GPT54_MINI_DEPLOYMENT,
    AZURE_ENV_GPT54_MINI_KEY,
    AZURE_ENV_GPT54_PRO_DEPLOYMENT,
    AZURE_ENV_GPT54_PRO_KEY,
)

AZURE_ENV_ALIASES = {
    AZURE_ENV_GPT54_MINI_KEY: ("AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"),
    AZURE_ENV_GPT54_MINI_DEPLOYMENT: ("AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_MODEL_DEPLOYMENT"),
    AZURE_ENV_GPT54_PRO_KEY: ("AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"),
    AZURE_ENV_GPT54_PRO_DEPLOYMENT: ("AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_MODEL_DEPLOYMENT"),
    AZURE_ENV_ENDPOINT: ("AZURE_OPENAI_BASE_URL",),
    AZURE_ENV_API_VERSION: ("OPENAI_API_VERSION",),
}

OPENAI_COMPATIBLE_BASE_URL_ENV = "OPENAI_BASE_URL"
OPENAI_COMPATIBLE_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_COMPATIBLE_MODEL_ENV = "OPENAI_MODEL"


def apply_azure_openai_env_aliases(env: dict[str, str] | None = None) -> dict[str, str]:
    target = os.environ if env is None else env
    for canonical, aliases in AZURE_ENV_ALIASES.items():
        if _value(target, canonical):
            continue
        for alias in aliases:
            alias_value = _value(target, alias)
            if alias_value:
                target[canonical] = alias_value
                break
    return target


def detect_azure_openai_routes(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    routes = [
        _route_status(
            route_id="azure_openai_gpt54_mini",
            endpoint_env=AZURE_ENV_ENDPOINT,
            deployment_env=AZURE_ENV_GPT54_MINI_DEPLOYMENT,
            key_env=AZURE_ENV_GPT54_MINI_KEY,
            source=source,
        ),
        _route_status(
            route_id="azure_openai_gpt54_pro",
            endpoint_env=AZURE_ENV_ENDPOINT,
            deployment_env=AZURE_ENV_GPT54_PRO_DEPLOYMENT,
            key_env=AZURE_ENV_GPT54_PRO_KEY,
            source=source,
        ),
        _route_status(
            route_id="azure_openai_gpt53_codex",
            endpoint_env=AZURE_ENV_ENDPOINT,
            deployment_env=AZURE_ENV_GPT53_CODEX_DEPLOYMENT,
            key_env=AZURE_ENV_GPT53_CODEX_KEY,
            source=source,
        ),
    ]
    present_envs: list[str] = []
    for route in routes:
        for env_name in route["present_envs"]:
            if env_name not in present_envs:
                present_envs.append(env_name)
    return {
        "routes": routes,
        "available_route_ids": [route["route_id"] for route in routes if route["available"]],
        "present_envs": present_envs,
        "any_route_available": any(route["available"] for route in routes),
    }


def build_openai_compatible_azure_gpt54_mini_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    route = detect_azure_openai_routes(source)["routes"][0]
    if not route["available"]:
        return {
            "available": False,
            "route_id": route["route_id"],
            "missing_envs": route["missing_envs"],
            "checked_env_groups": route["checked_env_groups"],
        }
    deployment = _resolved_value(source, AZURE_ENV_GPT54_MINI_DEPLOYMENT)
    endpoint = (_resolved_value(source, AZURE_ENV_ENDPOINT) or "").rstrip("/")
    api_key_env = route["resolved_envs"]["api_key_env"]
    return {
        "available": True,
        "route_id": route["route_id"],
        "deployment_name": deployment,
        "openai_compatible_env": {
            OPENAI_COMPATIBLE_BASE_URL_ENV: f"{endpoint}/openai/v1/",
            OPENAI_COMPATIBLE_API_KEY_ENV: f"${api_key_env}",
            OPENAI_COMPATIBLE_MODEL_ENV: deployment,
        },
        "source_env_names": {
            "endpoint_env": route["resolved_envs"]["endpoint_env"],
            "deployment_env": route["resolved_envs"]["deployment_env"],
            "api_key_env": api_key_env,
            "api_version_env": _resolved_name(source, AZURE_ENV_API_VERSION),
        },
        "checked_env_groups": route["checked_env_groups"],
    }


def build_openai_compatible_azure_gpt54_pro_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    route = detect_azure_openai_routes(source)["routes"][1]
    if not route["available"]:
        return {
            "available": False,
            "route_id": route["route_id"],
            "missing_envs": route["missing_envs"],
            "checked_env_groups": route["checked_env_groups"],
        }
    deployment = _resolved_value(source, AZURE_ENV_GPT54_PRO_DEPLOYMENT)
    endpoint = (_resolved_value(source, AZURE_ENV_ENDPOINT) or "").rstrip("/")
    api_key_env = route["resolved_envs"]["api_key_env"]
    return {
        "available": True,
        "route_id": route["route_id"],
        "deployment_name": deployment,
        "openai_compatible_env": {
            OPENAI_COMPATIBLE_BASE_URL_ENV: f"{endpoint}/openai/v1/",
            OPENAI_COMPATIBLE_API_KEY_ENV: f"${api_key_env}",
            OPENAI_COMPATIBLE_MODEL_ENV: deployment,
        },
        "source_env_names": {
            "endpoint_env": route["resolved_envs"]["endpoint_env"],
            "deployment_env": route["resolved_envs"]["deployment_env"],
            "api_key_env": api_key_env,
            "api_version_env": _resolved_name(source, AZURE_ENV_API_VERSION),
        },
        "checked_env_groups": route["checked_env_groups"],
    }


def _route_status(
    *,
    route_id: str,
    endpoint_env: str,
    deployment_env: str,
    key_env: str,
    source: Mapping[str, str],
) -> dict[str, Any]:
    resolved_envs = {
        "endpoint_env": _resolved_name(source, endpoint_env),
        "deployment_env": _resolved_name(source, deployment_env),
        "api_key_env": _resolved_name(source, key_env),
    }
    present_envs = [name for name in resolved_envs.values() if _value(source, name)]
    missing_envs = [name for name in resolved_envs.values() if not _value(source, name)]
    return {
        "route_id": route_id,
        "available": not missing_envs,
        "present_envs": present_envs,
        "missing_envs": missing_envs,
        "resolved_envs": resolved_envs,
        "checked_env_groups": {
            canonical: [canonical, *AZURE_ENV_ALIASES.get(canonical, ())]
            for canonical in (endpoint_env, deployment_env, key_env)
        },
    }


def _resolved_name(source: Mapping[str, str], canonical: str) -> str:
    if _value(source, canonical):
        return canonical
    for alias in AZURE_ENV_ALIASES.get(canonical, ()):
        if _value(source, alias):
            return alias
    return canonical


def _resolved_value(source: Mapping[str, str], canonical: str) -> str:
    return _value(source, _resolved_name(source, canonical))


def _value(source: Mapping[str, str], key: str) -> str:
    value = source.get(key, "")
    return value.strip() if isinstance(value, str) else ""
