"""Authority quarantine for timed-out Verifier generations."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import uuid
from typing import Any, Callable


class VerifierGenerationExpired(RuntimeError):
    pass


@dataclass
class VerifierGeneration:
    generation_id: str = field(default_factory=lambda: f"verifier-generation:{uuid.uuid4().hex}")
    _active: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _expired_reason: str = ""
    _quarantined_events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._active.set()

    @property
    def active(self) -> bool:
        return self._active.is_set()

    @property
    def expired_reason(self) -> str:
        return self._expired_reason

    def require_active(self) -> None:
        if not self.active:
            raise VerifierGenerationExpired(
                f"{self.generation_id} expired: {self._expired_reason or 'inactive'}"
            )

    def expire(self, reason: str) -> None:
        with self._lock:
            if self._active.is_set():
                self._expired_reason = str(reason)
                self._active.clear()

    def quarantine(self, kind: str, value: Any) -> None:
        with self._lock:
            self._quarantined_events.append({
                "generation_id": self.generation_id,
                "kind": str(kind),
                "value_type": type(value).__name__,
                "expired_reason": self._expired_reason,
            })

    def quarantined_snapshot(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._quarantined_events)


class GenerationBoundLedger:
    """Read-through ledger whose mutations require a live generation."""

    _MUTATING_METHODS = frozenset({
        "record",
        "record_accounting",
        "apply_verifier_result",
        "record_config_realization",
        "seed_capabilities",
        "ensure_objective",
    })

    def __init__(self, ledger: Any, generation: VerifierGeneration) -> None:
        object.__setattr__(self, "_ledger", ledger)
        object.__setattr__(self, "_generation", generation)

    @property
    def generation(self) -> VerifierGeneration:
        return object.__getattribute__(self, "_generation")

    @property
    def underlying_ledger(self) -> Any:
        return object.__getattribute__(self, "_ledger")

    def __getattr__(self, name: str) -> Any:
        ledger = object.__getattribute__(self, "_ledger")
        value = getattr(ledger, name)
        if name not in self._MUTATING_METHODS or not callable(value):
            return value

        generation = object.__getattribute__(self, "_generation")

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if not generation.active:
                generation.quarantine(name, args[0] if args else kwargs)
                return None
            return value(*args, **kwargs)

        return guarded

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        generation = object.__getattribute__(self, "_generation")
        if not generation.active:
            generation.quarantine(f"setattr:{name}", value)
            return
        setattr(object.__getattribute__(self, "_ledger"), name, value)
