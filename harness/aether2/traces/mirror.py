"""Progress mirror for HarnessEng Aether-2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from harness.aether2.traces.delta import DeltaReport

__all__ = ["Mirror", "MirrorNote", "SemanticObservation"]


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.strip().split()).lower()
    return normalized or None


def _normalize_target(value: str | None, target_kind: str | None) -> str | None:
    normalized = _normalize_text(value)
    if normalized is None:
        return None
    if target_kind != "path":
        return normalized
    candidate = normalized.replace("\\", "/")
    if candidate.startswith("./"):
        candidate = candidate[2:]
    while "//" in candidate:
        candidate = candidate.replace("//", "/")
    if candidate != "/":
        candidate = candidate.rstrip("/")
    return PurePosixPath(candidate).as_posix()


@dataclass(frozen=True)
class SemanticObservation:
    """Per-step semantic state for repeated-strategy detection."""

    action_family: str
    target: str | None = None
    target_kind: str | None = None
    failure_class_before: str | None = None
    failure_class_after: str | None = None
    requirement_advanced: bool = False
    stronger_evidence_added: bool = False
    artifact_evidence: tuple[str, ...] = ()
    meaningful_artifact_change: bool | None = None
    legitimate_polling: bool = False
    bounded_retry: bool = False

    def normalized(self, *, default_action_family: str | None = None) -> "SemanticObservation":
        action_family = _normalize_text(self.action_family) or _normalize_text(default_action_family) or "unknown"
        target_kind = _normalize_text(self.target_kind)
        return SemanticObservation(
            action_family=action_family,
            target=_normalize_target(self.target, target_kind),
            target_kind=target_kind,
            failure_class_before=_normalize_text(self.failure_class_before),
            failure_class_after=_normalize_text(self.failure_class_after),
            requirement_advanced=self.requirement_advanced,
            stronger_evidence_added=self.stronger_evidence_added,
            artifact_evidence=tuple(
                normalized_path
                for item in self.artifact_evidence
                if (normalized_path := _normalize_target(item, "path")) is not None
            ),
            meaningful_artifact_change=self.meaningful_artifact_change,
            legitimate_polling=self.legitimate_polling,
            bounded_retry=self.bounded_retry,
        )

    @property
    def has_meaningful_artifact_change(self) -> bool:
        if self.meaningful_artifact_change is not None:
            return self.meaningful_artifact_change
        return bool(self.artifact_evidence)


@dataclass(frozen=True)
class MirrorNote:
    """Factual no-delta observation surfaced to the model."""

    action_signature: str
    streak: int
    text: str
    fuel_gauge_text: str | None = None
    note_type: str = "no_delta_progress"
    strategy_family: str | None = None
    target: str | None = None
    target_kind: str | None = None
    failure_class_before: str | None = None
    failure_class_after: str | None = None


@dataclass(frozen=True)
class _SemanticTracker:
    action_family: str
    target: str | None
    target_kind: str | None
    failure_class: str
    count: int


class Mirror:
    """Track repeated no-progress patterns and emit factual observations."""

    def __init__(self) -> None:
        self._last_action_signature: str | None = None
        self._streak = 0
        self._semantic_tracker: _SemanticTracker | None = None

    @property
    def streak(self) -> int:
        return self._streak

    @property
    def repeated_failed_strategy_count(self) -> int:
        if self._semantic_tracker is None:
            return 0
        return self._semantic_tracker.count

    def observe(
        self,
        action_signature: str,
        delta: DeltaReport,
        *,
        semantic_observation: SemanticObservation | None = None,
        established_facts: list[str] | None = None,
        unused_affordances: list[str] | None = None,
        fuel_gauge_text: str | None = None,
    ) -> MirrorNote | None:
        if semantic_observation is not None:
            self._reset_legacy()
            return self._observe_semantic(
                action_signature=action_signature,
                semantic_observation=semantic_observation,
                fuel_gauge_text=fuel_gauge_text,
            )

        self._reset_semantic()
        if action_signature != self._last_action_signature:
            self._last_action_signature = action_signature
            self._streak = 0

        if not delta.is_empty:
            self._streak = 0
            return None

        self._streak += 1
        if self._streak == 3:
            return self._build_note(
                include_fuel_gauge=False,
                established_facts=established_facts or [],
                unused_affordances=unused_affordances or [],
                fuel_gauge_text=fuel_gauge_text,
            )
        if self._streak == 6:
            return self._build_note(
                include_fuel_gauge=True,
                established_facts=established_facts or [],
                unused_affordances=unused_affordances or [],
                fuel_gauge_text=fuel_gauge_text,
        )
        return None

    def _observe_semantic(
        self,
        *,
        action_signature: str,
        semantic_observation: SemanticObservation,
        fuel_gauge_text: str | None,
    ) -> MirrorNote | None:
        observation = semantic_observation.normalized(default_action_family=action_signature.partition(":")[0] or action_signature)
        if (
            observation.legitimate_polling
            or observation.bounded_retry
            or observation.requirement_advanced
            or observation.stronger_evidence_added
            or observation.has_meaningful_artifact_change
            or observation.failure_class_after is None
        ):
            self._reset_semantic()
            return None

        tracker = _SemanticTracker(
            action_family=observation.action_family,
            target=observation.target,
            target_kind=observation.target_kind,
            failure_class=observation.failure_class_after,
            count=1,
        )
        failure_persisted = (
            observation.failure_class_before is None
            or observation.failure_class_before == observation.failure_class_after
        )
        if (
            self._semantic_tracker is not None
            and failure_persisted
            and self._semantic_tracker.action_family == tracker.action_family
            and self._semantic_tracker.target == tracker.target
            and self._semantic_tracker.target_kind == tracker.target_kind
            and self._semantic_tracker.failure_class == tracker.failure_class
        ):
            tracker = _SemanticTracker(
                action_family=tracker.action_family,
                target=tracker.target,
                target_kind=tracker.target_kind,
                failure_class=tracker.failure_class,
                count=self._semantic_tracker.count + 1,
            )
        self._semantic_tracker = tracker

        if tracker.count < 3 or tracker.count % 3 != 0:
            return None
        return self._build_semantic_note(
            tracker=tracker,
            failure_class_before=observation.failure_class_before,
            fuel_gauge_text=fuel_gauge_text if tracker.count >= 6 else None,
        )

    def _build_note(
        self,
        *,
        include_fuel_gauge: bool,
        established_facts: list[str],
        unused_affordances: list[str],
        fuel_gauge_text: str | None,
    ) -> MirrorNote:
        facts_text = ", ".join(established_facts) if established_facts else "none recorded"
        affordances_text = ", ".join(unused_affordances) if unused_affordances else "none recorded"
        text = (
            f"Steps in this streak produced no state change. "
            f"Already established: {facts_text}. "
            f"Not yet tried: {affordances_text}."
        )
        return MirrorNote(
            action_signature="" if self._last_action_signature is None else self._last_action_signature,
            streak=self._streak,
            text=text,
            fuel_gauge_text=(fuel_gauge_text or "elapsed/remaining time") if include_fuel_gauge else None,
        )

    def _build_semantic_note(
        self,
        *,
        tracker: _SemanticTracker,
        failure_class_before: str | None,
        fuel_gauge_text: str | None,
    ) -> MirrorNote:
        target_text = ""
        if tracker.target is not None:
            target_label = tracker.target_kind or "target"
            target_text = f' on {target_label} "{tracker.target}"'
        text = (
            f'Observed {tracker.count} repeated failed attempts in strategy family "{tracker.action_family}"'
            f"{target_text}. "
            f'Failure class remained "{tracker.failure_class}". '
            "No requirement advanced, no stronger evidence was added, and no meaningful artifact or file evidence appeared. "
            "Please form a new hypothesis or switch to a different strategy family."
        )
        return MirrorNote(
            action_signature=tracker.action_family,
            streak=tracker.count,
            text=text,
            fuel_gauge_text=fuel_gauge_text,
            note_type="semantic_no_progress",
            strategy_family=tracker.action_family,
            target=tracker.target,
            target_kind=tracker.target_kind,
            failure_class_before=failure_class_before,
            failure_class_after=tracker.failure_class,
        )

    def _reset_legacy(self) -> None:
        self._last_action_signature = None
        self._streak = 0

    def _reset_semantic(self) -> None:
        self._semantic_tracker = None
