"""Generic, attributable accounting of cleanup and exit actions for owned jobs, processes, and sessions at run completion."""

# Given a registry of resources this run is recorded as owning and the
# observed liveness/exit state for each, this module classifies each resource
# into exactly one outcome bucket and produces a summary that the runner can
# record alongside the run result.
#
# The goal is truthful accounting: a resource that this run did not own and
# did not touch must never be reported as cleaned up, and a resource this run
# owned but could not stop must be reported as such rather than silently
# dropped.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


# Generic outcome buckets. Kept small and observable -- no task vocabulary.
CLEANUP_OUTCOMES = (
    "already_exited",
    "stopped_by_run",
    "stop_failed",
    "not_owned_by_run",
    "unknown_state",
)


@dataclass(frozen=True)
class ResourceCleanupRecord:
    resource_id: str
    resource_kind: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class CleanupAccounting:
    records: tuple[ResourceCleanupRecord, ...]

    @property
    def counts(self) -> dict[str, int]:
        counts = {outcome: 0 for outcome in CLEANUP_OUTCOMES}
        for record in self.records:
            counts[record.outcome] = counts.get(record.outcome, 0) + 1
        return counts

    @property
    def attempted_count(self) -> int:
        return sum(
            1
            for record in self.records
            if record.outcome in {"stopped_by_run", "stop_failed"}
        )

    @property
    def unexplained_count(self) -> int:
        """Resources whose cleanup outcome cannot be attributed to this run.

        A non-zero value here indicates the cleanup pass left state it cannot
        explain -- the runner should surface this rather than silently
        treating the run as fully clean.
        """

        return self.counts.get("unknown_state", 0) + self.counts.get("stop_failed", 0)

    @property
    def is_fully_attributable(self) -> bool:
        return self.unexplained_count == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": [
                {
                    "resource_id": record.resource_id,
                    "resource_kind": record.resource_kind,
                    "outcome": record.outcome,
                    "detail": record.detail,
                }
                for record in self.records
            ],
            "counts": self.counts,
            "attempted_count": self.attempted_count,
            "unexplained_count": self.unexplained_count,
            "is_fully_attributable": self.is_fully_attributable,
        }


def account_for_cleanup(
    owned_resources: Iterable[Mapping[str, Any]],
    *,
    observed_state: Mapping[str, Mapping[str, Any]],
    stop_results: Mapping[str, Mapping[str, Any]] | None = None,
) -> CleanupAccounting:
    """Classify each owned resource's cleanup outcome.

    `owned_resources`: iterable of mappings with at least `resource_id` and
    `resource_kind` (e.g. "job", "process", "session"). Only resources this
    run recorded as owning are considered -- this function never inspects or
    reports on anything outside `owned_resources`.

    `observed_state`: mapping of resource_id -> {"alive": bool, ...} observed
    BEFORE any stop attempt.

    `stop_results`: optional mapping of resource_id -> {"attempted": bool,
    "alive_after": bool, ...} for resources a stop was attempted on. A
    resource present in `owned_resources` but absent from `observed_state`
    yields `unknown_state` (truthful: this run cannot account for it).
    """

    stop_results = stop_results or {}
    records: list[ResourceCleanupRecord] = []
    for resource in owned_resources:
        resource_id = str(resource.get("resource_id", "")).strip()
        resource_kind = str(resource.get("resource_kind", "")).strip() or "unknown"
        if not resource_id:
            records.append(
                ResourceCleanupRecord(
                    resource_id="",
                    resource_kind=resource_kind,
                    outcome="unknown_state",
                    detail="resource record missing resource_id",
                )
            )
            continue

        before = observed_state.get(resource_id)
        if before is None:
            records.append(
                ResourceCleanupRecord(
                    resource_id=resource_id,
                    resource_kind=resource_kind,
                    outcome="unknown_state",
                    detail="no observed pre-cleanup state for owned resource",
                )
            )
            continue

        if not bool(before.get("alive", False)):
            records.append(
                ResourceCleanupRecord(
                    resource_id=resource_id,
                    resource_kind=resource_kind,
                    outcome="already_exited",
                    detail="resource was not alive before cleanup",
                )
            )
            continue

        stop = stop_results.get(resource_id)
        if stop is None:
            records.append(
                ResourceCleanupRecord(
                    resource_id=resource_id,
                    resource_kind=resource_kind,
                    outcome="unknown_state",
                    detail="owned live resource had no recorded stop attempt",
                )
            )
            continue

        if not bool(stop.get("attempted", False)):
            records.append(
                ResourceCleanupRecord(
                    resource_id=resource_id,
                    resource_kind=resource_kind,
                    outcome="unknown_state",
                    detail="stop was recorded but not attempted",
                )
            )
            continue

        if bool(stop.get("alive_after", True)):
            records.append(
                ResourceCleanupRecord(
                    resource_id=resource_id,
                    resource_kind=resource_kind,
                    outcome="stop_failed",
                    detail="resource still alive after attempted stop",
                )
            )
            continue

        records.append(
            ResourceCleanupRecord(
                resource_id=resource_id,
                resource_kind=resource_kind,
                outcome="stopped_by_run",
                detail="resource stopped by this run's cleanup pass",
            )
        )

    return CleanupAccounting(records=tuple(records))


def classify_unowned_state(
    *,
    owned_resource_ids: Iterable[str],
    observed_live_resource_ids: Iterable[str],
) -> tuple[str, ...]:
    """Return resource ids observed live but not owned by this run.

    These must never be reported as cleaned up by this run (no attributable
    cleanup), but surfacing them lets the runner report truthful "left
    behind, not ours" state instead of silently ignoring it.
    """

    owned = {str(item) for item in owned_resource_ids}
    live = {str(item) for item in observed_live_resource_ids}
    return tuple(sorted(live - owned))
