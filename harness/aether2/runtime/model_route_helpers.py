"""TPM-pacer option extraction, scalar coercion, and route utility helpers.

Extracted from model_routes.py to keep that module under 500 LOC.
These are internal helpers; callers should import from model_routes.py.
"""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from harness.aether2.runtime.model_routes import ModelClient

_TPM_PACER_SETTING_KEYS = frozenset(
    {
        "tpm_pacer_enabled",
        "tpm_limit",
        "tpm_window_sec",
        "tpm_throttle_fraction",
        "tpm_pause_sec",
        "tpm_count_mode",
        "rpm_limit",
        "rpm_window_sec",
        "model_max_concurrency",
    }
)


def _required_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required environment variable: {name}")
    return value.strip()


def _bool_env(name: str, *, default: bool) -> bool:
    return _as_bool(os.environ.get(name), default=default)


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def _as_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_non_negative_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _model_route_pacer_scope(route: dict[str, Any]) -> str:
    settings = route.get("request_settings")
    settings = settings if isinstance(settings, dict) else {}
    return "|".join(
        [
            str(route.get("provider_route") or "provider"),
            str(route.get("model_client_id") or "client"),
            str(route.get("model_name") or "model"),
            str(settings.get("azure_deployment") or ""),
            str(route.get("api_base") or ""),
        ]
    )


def _route_without_tpm_pacer_settings(route: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(route)
    settings = cleaned.get("request_settings")
    if isinstance(settings, dict):
        cleaned["request_settings"] = {
            key: value for key, value in settings.items() if key not in _TPM_PACER_SETTING_KEYS
        }
    return cleaned


def _extract_tpm_pacer_options(route: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    settings = route.get("request_settings")
    settings = dict(settings) if isinstance(settings, dict) else {}

    def _pop_or_setting(key: str, default: Any = None) -> Any:
        if key in kwargs:
            return kwargs.pop(key)
        return settings.get(key, default)

    enabled = _bool_env("HARNESS_TPM_PACER_ENABLED", default=False)
    enabled = _as_bool(_pop_or_setting("tpm_pacer_enabled", enabled), default=enabled)
    return {
        "enabled": enabled,
        "tpm_limit": _as_positive_int(
            _pop_or_setting("tpm_limit", os.environ.get("HARNESS_TPM_LIMIT", "100000")),
            default=100000,
        ),
        "window_sec": _as_positive_float(
            _pop_or_setting("tpm_window_sec", os.environ.get("HARNESS_TPM_WINDOW_SEC", "60")),
            default=60.0,
        ),
        "throttle_fraction": _as_positive_float(
            _pop_or_setting("tpm_throttle_fraction", os.environ.get("HARNESS_TPM_THROTTLE_FRACTION", "0.85")),
            default=0.85,
        ),
        "pause_sec": _as_non_negative_float(
            _pop_or_setting("tpm_pause_sec", os.environ.get("HARNESS_TPM_PAUSE_SEC", "4")),
            default=4.0,
        ),
        "token_count_mode": str(
            _pop_or_setting("tpm_count_mode", os.environ.get("HARNESS_TPM_COUNT_MODE", "total"))
            or "total"
        ),
        "rpm_limit": _as_optional_positive_int(
            _pop_or_setting("rpm_limit", os.environ.get("HARNESS_RPM_LIMIT")),
        ),
        "rpm_window_sec": _as_positive_float(
            _pop_or_setting("rpm_window_sec", os.environ.get("HARNESS_RPM_WINDOW_SEC", "60")),
            default=60.0,
        ),
        "max_concurrency": _as_positive_int(
            _pop_or_setting("model_max_concurrency", os.environ.get("HARNESS_MODEL_MAX_CONCURRENCY", "1")),
            default=1,
        ),
        "shared_scope": _model_route_pacer_scope(route),
    }


def _maybe_wrap_tpm_pacer(client: "ModelClient", options: dict[str, Any]) -> "ModelClient":
    if not options.get("enabled"):
        return client
    from harness.aether2.runtime.tpm_pacer import make_paced_client

    return make_paced_client(
        client,
        tpm_limit=int(options["tpm_limit"]),
        window_sec=float(options["window_sec"]),
        throttle_fraction=float(options["throttle_fraction"]),
        pause_sec=float(options["pause_sec"]),
        token_count_mode=str(options["token_count_mode"]),
        rpm_limit=options.get("rpm_limit"),
        rpm_window_sec=float(options["rpm_window_sec"]),
        max_concurrency=int(options["max_concurrency"]),
        shared_scope=str(options["shared_scope"]),
        enabled=True,
    )
