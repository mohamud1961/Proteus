"""Canonical immutable environment and atomic dynamic world state."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

from .receipts import OutputHandleStore, ReceiptStore
from .task_contract import TaskContract


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


class WorldStateDeltaError(ValueError):
    """Raised when a dynamic state update is malformed."""


@dataclass(frozen=True)
class StableEnvMap:
    version: int
    _facts_json: str
    sha256: str

    def __post_init__(self) -> None:
        """Reject forged or non-canonical direct constructions.

        ``create`` remains the normal construction API, but the dataclass is
        imported by callers and can otherwise be instantiated directly.  A
        frozen dataclass prevents later mutation, not inconsistent initial
        values, so validate the complete content-addressed identity here.
        """
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("EnvMap version must be a positive integer")
        if not isinstance(self._facts_json, str):
            raise TypeError("EnvMap facts encoding must be a string")
        try:
            facts = json.loads(self._facts_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("EnvMap facts encoding must be valid JSON") from exc
        if not isinstance(facts, Mapping):
            raise ValueError("EnvMap facts encoding must contain a mapping")
        if _canonical(facts) != self._facts_json:
            raise ValueError("EnvMap facts encoding must be canonical JSON")
        expected = hashlib.sha256(_canonical({"version": self.version, "facts": facts}).encode()).hexdigest()
        if not isinstance(self.sha256, str) or self.sha256 != expected:
            raise ValueError("EnvMap digest does not match version and facts")

    @classmethod
    def create(cls, facts: Mapping[str, Any], *, version: int = 1) -> "StableEnvMap":
        if version <= 0:
            raise ValueError("EnvMap version must be positive")
        if not isinstance(facts, Mapping):
            raise TypeError("EnvMap facts must be a mapping")
        encoded = _canonical(deepcopy(dict(facts)))
        digest = hashlib.sha256(_canonical({"version": version, "facts": json.loads(encoded)}).encode()).hexdigest()
        return cls(version, encoded, digest)

    @property
    def facts(self) -> dict[str, Any]:
        return json.loads(self._facts_json)

    def to_payload(self) -> dict[str, Any]:
        return {"schema_version": "stable_envmap.v1", "version": self.version, "sha256": self.sha256, "facts": self.facts}

    def revise(self, *, changes: Mapping[str, Any], reason: str, evidence_receipt_ids: Sequence[str]) -> "StableEnvMap":
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("EnvMap revision requires a non-empty reason")
        if not evidence_receipt_ids:
            raise ValueError("EnvMap revision requires at least one evidence receipt")
        if not isinstance(changes, Mapping) or not changes:
            raise ValueError("EnvMap revision requires at least one changed fact")
        merged = self.facts
        _deep_merge(merged, deepcopy(dict(changes)))
        if _canonical(merged) == self._facts_json:
            raise ValueError("EnvMap revision must materially change facts")
        return StableEnvMap.create(merged, version=self.version + 1)


def _deep_merge(target: dict[str, Any], update: Mapping[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), Mapping):
            nested = dict(target[key])
            _deep_merge(nested, value)
            target[key] = nested
        else:
            target[key] = deepcopy(value)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorldStateDeltaError(f"{name} must be a mapping")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise WorldStateDeltaError(f"{name} must be a sequence")
    return value


@dataclass
class WorldState:
    task_contract: TaskContract
    stable_envmap: StableEnvMap | None = None
    env_facts: dict[str, Any] = field(default_factory=dict)
    installed_packages: dict[str, str] = field(default_factory=dict)
    files: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)
    jobs: dict[str, Any] = field(default_factory=dict)
    active_findings: list[Mapping[str, Any]] = field(default_factory=list)
    latest_result: Mapping[str, Any] | None = None
    named_sections: dict[str, Any] = field(default_factory=dict)
    # Tombstones are part of dynamic state.  Keeping them separate from the
    # live mappings lets a state-only Verifier distinguish an explicit remove
    # from a service/job that was never observed.
    removed_services: list[str] = field(default_factory=list)
    removed_jobs: list[str] = field(default_factory=list)
    state_version: int = 0
    receipts: ReceiptStore = field(default_factory=ReceiptStore)
    output_handles: OutputHandleStore | None = None

    def __post_init__(self) -> None:
        if self.stable_envmap is None:
            self.stable_envmap = StableEnvMap.create(self.env_facts or {})
        if self.output_handles is None:
            self.output_handles = OutputHandleStore(self.receipts)

    def store_output(self, content: str | bytes, *, kind: str = "output") -> str:
        assert self.output_handles is not None
        return self.output_handles.put(content, kind=kind)

    def retrieve_output(self, handle: str) -> str | bytes:
        assert self.output_handles is not None
        return self.output_handles.get(handle)

    def dynamic_snapshot(self) -> dict[str, Any]:
        def compact(value: Any) -> Any:
            if isinstance(value, str):
                # Preserve change detection without replaying the payload inline.
                # Length alone would incorrectly classify same-sized replacements
                # as no-ops and suppress a state-version increment.
                return {
                    "status": "present",
                    "chars": len(value),
                    "sha256": hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest(),
                }
            if isinstance(value, Mapping):
                allowed = {"status", "step", "sha256", "bytes", "chars", "size", "modified_at", "state", "pid", "port", "returncode", "readiness", "stdout_handle", "stderr_handle", "handle", "file_handle"}
                row = {str(k): deepcopy(v) for k, v in value.items() if k in allowed}
                if not row:
                    row = {"status": "present", "keys": sorted(str(k) for k in value)}
                else:
                    omitted = sorted(str(k) for k in value if k not in allowed)
                    if omitted:
                        row["omitted_keys"] = omitted
                # A digest makes omitted fields and same-shaped updates visible
                # to atomic no-op detection while keeping large values compact.
                row["sha256"] = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
                return row
            return {
                "status": "present",
                "type": type(value).__name__,
                "sha256": hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest(),
            }
        return {
            "schema_version": "dynamic_world_state.v1",
            "state_version": self.state_version,
            "installed_packages": deepcopy(self.installed_packages),
            "runtime_facts": deepcopy(self.env_facts),
            "files": {str(k): compact(v) for k, v in self.files.items()},
            "services": {str(k): compact(v) for k, v in self.services.items()},
            "jobs": {str(k): compact(v) for k, v in self.jobs.items()},
            "artifacts": {str(k): compact(v) for k, v in self.artifacts.items()},
            "latest_result": compact(self.latest_result) if self.latest_result is not None else None,
            "active_findings": deepcopy(self.active_findings),
            "named_sections": deepcopy(self.named_sections),
            "removed_services": sorted(set(self.removed_services)),
            "removed_jobs": sorted(set(self.removed_jobs)),
        }

    def apply_delta(self, delta: Mapping[str, Any], *, step: int | None = None) -> tuple[str, ...]:
        if not isinstance(delta, Mapping):
            raise WorldStateDeltaError("dynamic-state delta must be a mapping")
        candidate = WorldState(
            task_contract=self.task_contract,
            stable_envmap=self.stable_envmap,
            env_facts=deepcopy(self.env_facts), installed_packages=deepcopy(self.installed_packages),
            files=deepcopy(self.files), artifacts=deepcopy(self.artifacts), services=deepcopy(self.services),
            jobs=deepcopy(self.jobs), active_findings=deepcopy(self.active_findings), latest_result=deepcopy(self.latest_result),
            named_sections=deepcopy(self.named_sections),
            removed_services=deepcopy(self.removed_services), removed_jobs=deepcopy(self.removed_jobs),
            state_version=self.state_version,
            receipts=self.receipts, output_handles=self.output_handles,
        )
        before = candidate.dynamic_snapshot(), deepcopy(candidate.named_sections)
        messages = candidate._apply_delta_in_place(delta, step=step)
        after = candidate.dynamic_snapshot(), deepcopy(candidate.named_sections)
        if before == after:
            return tuple()
        for name in ("env_facts", "installed_packages", "files", "artifacts", "services", "jobs", "named_sections", "active_findings", "removed_services", "removed_jobs"):
            setattr(self, name, getattr(candidate, name))
        self.state_version += 1
        return messages

    def _apply_delta_in_place(self, delta: Mapping[str, Any], *, step: int | None) -> tuple[str, ...]:
        allowed = {"installed_packages", "runtime_facts", "files", "services", "jobs", "artifacts", "latest_result", "named_sections", "active_findings", "removed_services", "removed_jobs"}
        unknown = sorted(set(delta) - allowed)
        if unknown:
            raise WorldStateDeltaError(f"unsupported dynamic-state keys: {unknown}")
        messages: list[str] = []
        if "installed_packages" in delta:
            for name, version in _mapping(delta["installed_packages"], "installed_packages").items():
                self.installed_packages[str(name)] = str(version)
                messages.append(f"{name} {version} has now been installed.")
        if "runtime_facts" in delta:
            facts = _mapping(delta["runtime_facts"], "runtime_facts")
            _deep_merge(self.env_facts, facts)
            messages.extend(f"Runtime fact {key} is now {value}." for key, value in facts.items())
        if "files" in delta:
            for path, value in _mapping(delta["files"], "files").items():
                path_text = str(path)
                prior = self.files.get(path_text)
                if isinstance(value, Mapping):
                    next_value = dict(prior) if isinstance(prior, Mapping) else {}
                    _deep_merge(next_value, value)
                    status = str(value.get("status", "modified"))
                    effective_step = value.get("step", step)
                    self.files[path_text] = next_value
                else:
                    self.files[path_text] = deepcopy(value)
                    status = "modified" if prior is not None else "created"
                    effective_step = step
                name = path_text.rsplit("/", 1)[-1] or path_text
                suffix = f" at step {effective_step}" if effective_step is not None else ""
                messages.append(f"{name} was {status}{suffix}.")
        for field_name, label in (("services", "Service"), ("jobs", "Job")):
            if field_name not in delta:
                continue
            target = getattr(self, field_name)
            for identifier, value in _mapping(delta[field_name], field_name).items():
                if not isinstance(value, Mapping):
                    raise WorldStateDeltaError(f"{field_name[:-1]} {identifier} must be a mapping")
                merged = dict(target.get(str(identifier), {}))
                _deep_merge(merged, value)
                target[str(identifier)] = merged
                tombstones = self.removed_services if field_name == "services" else self.removed_jobs
                if str(identifier) in tombstones:
                    tombstones.remove(str(identifier))
                state = merged.get("state", "updated")
                if field_name == "services" and state == "listening" and merged.get("port") is not None:
                    messages.append(f"Process {identifier} is listening on {merged['port']}.")
                else:
                    messages.append(f"{label} {identifier} is now {state}.")
        if "artifacts" in delta:
            for path, value in _mapping(delta["artifacts"], "artifacts").items():
                self.artifacts[str(path)] = deepcopy(value)
                messages.append(f"Artifact {path} is now available.")
        if "latest_result" in delta:
            latest = delta["latest_result"]
            if not isinstance(latest, Mapping):
                raise WorldStateDeltaError("latest_result must be a mapping")
            self.latest_result = deepcopy(dict(latest))
            messages.append("Latest runtime result was updated.")
        if "named_sections" in delta:
            _deep_merge(self.named_sections, _mapping(delta["named_sections"], "named_sections"))
        if "active_findings" in delta:
            findings = _sequence(delta["active_findings"], "active_findings")
            if any(not isinstance(item, Mapping) for item in findings):
                bad = next(i for i, item in enumerate(findings) if not isinstance(item, Mapping))
                raise WorldStateDeltaError(f"active_findings[{bad}] must be a mapping")
            self.active_findings = [deepcopy(dict(item)) for item in findings]
            messages.append(f"There are now {len(self.active_findings)} active Verifier findings.")
        for identifier in _sequence(delta.get("removed_services", ()), "removed_services"):
            self.services.pop(str(identifier), None)
            if str(identifier) not in self.removed_services:
                self.removed_services.append(str(identifier))
            messages.append(f"Service {identifier} is no longer active.")
        for identifier in _sequence(delta.get("removed_jobs", ()), "removed_jobs"):
            self.jobs.pop(str(identifier), None)
            if str(identifier) not in self.removed_jobs:
                self.removed_jobs.append(str(identifier))
            messages.append(f"Job {identifier} is no longer active.")
        return tuple(messages)
