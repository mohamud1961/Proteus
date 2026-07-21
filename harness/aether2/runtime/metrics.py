"""Per-trial scorecard helpers for Aether-2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.aether2.runtime.action_bus import extract_command, infer_action_type


@dataclass(frozen=True)
class Scorecard:
    verifier_clean: bool
    steps: int
    model_calls: int
    tokens_cached: int
    tokens_fresh: int
    cost: float
    wall_time: float
    cache_hit_ratio: float
    no_delta_streaks: int
    verification_rounds: int
    recoveries: int
    compaction_count: int
    job_survival: bool
    session_survival: bool
    grader_reward: float | None = None
    action_type_breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def verifier_readiness(self) -> bool:
        """Advisory verifier readiness signal; official reward remains authoritative."""
        return self.verifier_clean

    @property
    def pass_(self) -> bool:
        """Deprecated alias for the advisory verifier readiness signal."""
        return self.verifier_clean

    def as_dict(self) -> dict[str, Any]:
        return {
            "verifier_readiness": self.verifier_readiness,
            "verifier_clean": self.verifier_clean,
            "grader_reward": self.grader_reward,
            "steps": self.steps,
            "model_calls": self.model_calls,
            "tokens_cached": self.tokens_cached,
            "tokens_fresh": self.tokens_fresh,
            "cost": self.cost,
            "wall_time": self.wall_time,
            "cache_hit_ratio": self.cache_hit_ratio,
            "no_delta_streaks": self.no_delta_streaks,
            "verification_rounds": self.verification_rounds,
            "recoveries": self.recoveries,
            "compaction_count": self.compaction_count,
            "job_survival": self.job_survival,
            "session_survival": self.session_survival,
            "action_type_breakdown": dict(self.action_type_breakdown),
        }


def _get_value(run: Any, name: str, default: Any = None) -> Any:
    if isinstance(run, dict):
        return run.get(name, default)
    return getattr(run, name, default)


def _record_value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _get_int(run: Any, *names: str, default: int = 0) -> int:
    for name in names:
        value = _get_value(run, name, None)
        if value is not None:
            return int(value)
    return default


def _get_float(run: Any, *names: str, default: float = 0.0) -> float:
    for name in names:
        value = _get_value(run, name, None)
        if value is not None:
            return float(value)
    return default


def _get_bool(run: Any, *names: str, default: bool = False) -> bool:
    for name in names:
        value = _get_value(run, name, None)
        if value is not None:
            return bool(value)
    return default


def build_scorecard(run: Any) -> Scorecard:
    """Build a scorecard from a synthetic or real run object."""

    tokens_cached = _get_int(run, "tokens_cached", "cached_tokens")
    tokens_fresh = _get_int(run, "tokens_fresh", "fresh_tokens")
    total_tokens = tokens_cached + tokens_fresh
    cache_hit_ratio = (tokens_cached / total_tokens) if total_tokens else 0.0
    action_type_breakdown = _build_action_type_breakdown(run)

    grader_reward_raw = _get_value(run, "grader_reward", None)
    grader_reward = float(grader_reward_raw) if grader_reward_raw is not None else None

    return Scorecard(
        verifier_clean=_get_bool(run, "verifier_readiness", "verifier_clean", "pass_", "pass", "passed", "success"),
        grader_reward=grader_reward,
        steps=_get_int(run, "steps", "step_count"),
        model_calls=_get_int(run, "model_calls", "model_call_count"),
        tokens_cached=tokens_cached,
        tokens_fresh=tokens_fresh,
        cost=_get_float(run, "cost", "total_cost"),
        wall_time=_get_float(run, "wall_time", "wall_time_sec"),
        cache_hit_ratio=cache_hit_ratio,
        no_delta_streaks=_get_int(run, "no_delta_streaks", "mirror_no_delta_streaks"),
        verification_rounds=_get_int(run, "verification_rounds", "verify_rounds"),
        recoveries=_get_int(run, "recoveries", "recovery_count"),
        compaction_count=_get_int(run, "compaction_count", "compactions"),
        job_survival=_get_bool(run, "job_survival", "jobs_survived"),
        session_survival=_get_bool(run, "session_survival", "sessions_survived"),
        action_type_breakdown=action_type_breakdown,
    )


def _build_action_type_breakdown(run: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in _extract_tool_invocations(run):
        tool_name = _record_tool_name(record)
        command = _record_command(record)
        action_type = infer_action_type(tool_name=tool_name, command=command)
        counts[action_type] = counts.get(action_type, 0) + 1
    return dict(sorted(counts.items()))


def _extract_tool_invocations(run: Any) -> list[Any]:
    for name in ("tool_invocations", "action_records", "tool_calls", "actions", "recorded_actions"):
        value = _get_value(run, name, None)
        if isinstance(value, list):
            return value

    action_summary = _get_value(run, "action_summary", None)
    if action_summary is not None:
        records = _record_value(action_summary, "records", None)
        if isinstance(records, list):
            return records

    records = _get_value(run, "records", None)
    if isinstance(records, list) and any(_is_action_like_record(item) for item in records):
        return records
    return []


def _is_action_like_record(record: Any) -> bool:
    if isinstance(record, dict):
        return any(key in record for key in ("tool_name", "name", "command", "arguments", "tool"))
    return any(hasattr(record, attr) for attr in ("tool_name", "name", "command", "arguments", "tool"))


def _record_tool_name(record: Any) -> str:
    for name in ("tool_name", "name", "tool"):
        value = _record_value(record, name, None)
        if isinstance(value, str) and value:
            return value
    return "raw_bash"


def _record_command(record: Any) -> str:
    value = _record_value(record, "command", None)
    if isinstance(value, str):
        return value
    if value is not None:
        return extract_command(value)
    arguments = _record_value(record, "arguments", None)
    if arguments is not None:
        return extract_command(arguments)
    tool_call = _record_value(record, "tool_call", None)
    if tool_call is not None:
        tool_call_command = _record_value(tool_call, "command", None)
        if isinstance(tool_call_command, str):
            return tool_call_command
        tool_call_arguments = _record_value(tool_call, "arguments", None)
        if tool_call_arguments is not None:
            return extract_command(tool_call_arguments)
    return ""
