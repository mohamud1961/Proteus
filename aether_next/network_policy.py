"""Environment-declared network policy for certified task containers."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping


LOOPBACK_ONLY = "loopback_only"
EXTERNAL_UNRESTRICTED = "external_unrestricted"
SUPPORTED_NETWORK_SCOPES = frozenset({LOOPBACK_ONLY, EXTERNAL_UNRESTRICTED})

_ALIASES = {
    "local_only": LOOPBACK_ONLY,
    "loopback": LOOPBACK_ONLY,
    "none": LOOPBACK_ONLY,
    "isolated": LOOPBACK_ONLY,
    "external": EXTERNAL_UNRESTRICTED,
    "unrestricted": EXTERNAL_UNRESTRICTED,
    "open": EXTERNAL_UNRESTRICTED,
}


@dataclass(frozen=True)
class ResolvedNetworkPolicy:
    scope: str
    source: str

    @property
    def docker_args(self) -> tuple[str, ...]:
        # Docker's `none` driver preserves the container loopback interface but
        # provides no external interface or route.
        return ("--network", "none") if self.scope == LOOPBACK_ONLY else ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "source": self.source,
            "docker_args": list(self.docker_args),
            "mechanical_boundary": (
                "docker_network_none" if self.scope == LOOPBACK_ONLY
                else "docker_default_bridge_explicitly_authorized"
            ),
        }


def _normalise_scope(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    value = _ALIASES.get(value, value)
    if value not in SUPPORTED_NETWORK_SCOPES:
        raise ValueError(
            f"unsupported network scope {raw!r}; expected one of "
            f"{sorted(SUPPORTED_NETWORK_SCOPES)}"
        )
    return value


def resolve_network_policy(
    task_metadata: Mapping[str, Any] | None,
    *,
    explicit_scope: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedNetworkPolicy:
    """Resolve policy without interpreting task semantics.

    Precedence: explicit runner input, public environment metadata, operator
    environment, then the certified-safe loopback-only baseline.
    """
    if explicit_scope is not None and str(explicit_scope).strip():
        return ResolvedNetworkPolicy(_normalise_scope(explicit_scope), "runner_argument")
    metadata = dict(task_metadata or {})
    environment = metadata.get("environment") if isinstance(metadata.get("environment"), Mapping) else {}
    for key, source in (
        (environment.get("network_scope"), "task_environment.network_scope"),
        (metadata.get("network_scope"), "task_metadata.network_scope"),
    ):
        if key is not None and str(key).strip():
            return ResolvedNetworkPolicy(_normalise_scope(key), source)
    env = os.environ if environ is None else environ
    operator = str(env.get("AETHER_TASK_NETWORK_SCOPE", "")).strip()
    if operator:
        return ResolvedNetworkPolicy(_normalise_scope(operator), "AETHER_TASK_NETWORK_SCOPE")
    return ResolvedNetworkPolicy(LOOPBACK_ONLY, "certified_default")
