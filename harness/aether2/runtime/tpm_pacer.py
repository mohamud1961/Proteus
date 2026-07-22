"""Rolling-TPM speed-bump pacer for model clients.

Wraps any ModelClient and enforces a sliding-window (default 60 s) token
budget.  When cumulative output tokens in that window approach the configured
threshold the wrapper sleeps *before* dispatching the next call to flatten the
spike.  The agent has no concept of wall-clock time between API calls; the
pause is fully transparent to model context.

Usage::

    from harness.aether2.runtime.tpm_pacer import RollingTPMPacer
    from harness.aether2.runtime.model_routes import make_model_client_from_route

    raw_client = make_model_client_from_route(route)
    paced_client = RollingTPMPacer(
        client=raw_client,
        tpm_limit=100_000,       # Azure deployment TPM cap
        window_sec=60.0,
        throttle_fraction=0.85,  # start pacing at 85 % of limit
        pause_sec=4.0,           # sleep duration per bump
        enabled=True,
    )
    result = paced_client.complete(messages)

The pacer reads ``usage.output_tokens`` (or ``usage.completion_tokens``) from
the response and records the observation timestamp.  Only output tokens are
counted because input tokens are re-sent on every turn and would massively
over-count RPM-based limits; callers that know their deployment quota for
*total* tokens can lower ``tpm_limit`` accordingly.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Deque

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rolling window token tracker
# ---------------------------------------------------------------------------

@dataclass
class _TokenEvent:
    """A single usage observation anchored to a monotonic timestamp."""
    timestamp: float   # time.monotonic()
    tokens: int


class RollingTokenWindow:
    """Maintain a sliding-window sum of token counts (thread-unsafe, single-harness use)."""

    def __init__(self, window_sec: float = 60.0) -> None:
        self.window_sec = max(1.0, float(window_sec))
        self._events: Deque[_TokenEvent] = collections.deque()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record(self, tokens: int, *, ts: float | None = None) -> None:
        """Record *tokens* at the given monotonic timestamp (default: now)."""
        if tokens <= 0:
            return
        if ts is None:
            ts = time.monotonic()
        self._events.append(_TokenEvent(timestamp=ts, tokens=tokens))
        self._evict(ts)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def rolling_total(self, *, ts: float | None = None) -> int:
        """Return the sum of tokens recorded in the last *window_sec* seconds."""
        if ts is None:
            ts = time.monotonic()
        self._evict(ts)
        return sum(e.tokens for e in self._events)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()


_SHARED_LOCK = threading.Lock()
_SHARED_TOKEN_WINDOWS: dict[str, RollingTokenWindow] = {}
_SHARED_REQUEST_WINDOWS: dict[str, RollingTokenWindow] = {}
_SHARED_SCOPE_LOCKS: dict[str, threading.Lock] = {}
_SHARED_SEMAPHORES: dict[tuple[str, int], threading.Semaphore] = {}


def _shared_window(registry: dict[str, RollingTokenWindow], scope: str, window_sec: float) -> RollingTokenWindow:
    with _SHARED_LOCK:
        window = registry.get(scope)
        if window is None or float(window.window_sec) != float(max(1.0, window_sec)):
            window = RollingTokenWindow(window_sec=window_sec)
            registry[scope] = window
        return window


def _shared_scope_lock(scope: str) -> threading.Lock:
    with _SHARED_LOCK:
        lock = _SHARED_SCOPE_LOCKS.get(scope)
        if lock is None:
            lock = threading.Lock()
            _SHARED_SCOPE_LOCKS[scope] = lock
        return lock


def _shared_semaphore(scope: str, max_concurrency: int) -> threading.Semaphore:
    with _SHARED_LOCK:
        key = (scope, max_concurrency)
        semaphore = _SHARED_SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.Semaphore(max_concurrency)
            _SHARED_SEMAPHORES[key] = semaphore
        return semaphore


# ---------------------------------------------------------------------------
# Main pacer class
# ---------------------------------------------------------------------------

@dataclass
class RollingTPMPacer:
    """Transparent ModelClient wrapper that adds rolling-TPM speed bumps.

    Parameters
    ----------
    client:
        The real ModelClient to delegate to.
    tpm_limit:
        The Azure/provider TPM quota for this deployment.  When the rolling
        window total exceeds ``tpm_limit * throttle_fraction`` the pacer sleeps
        ``pause_sec`` before dispatching the next request.
    window_sec:
        Observation window in seconds (default 60).
    throttle_fraction:
        Fraction of *tpm_limit* that triggers a pause (default 0.85 → 85 %).
    pause_sec:
        How long to sleep per speed bump (default 4.0 s).
    token_count_mode:
        Which usage field to meter. ``total`` is safest for provider TPM
        enforcement; ``output`` preserves the original speed-bump behavior.
    rpm_limit:
        Optional request-per-minute cap for providers that 429 on request rate
        rather than token volume.
    shared_scope:
        Optional key used to share rolling windows across client instances in a
        board run. Without this, per-row clients each start with an empty window.
    enabled:
        Master switch.  Set ``False`` to pass calls straight through with zero
        overhead (useful in unit tests or when the deployment has no quota).
    """

    client: Any  # ModelClient protocol
    tpm_limit: int = 100_000
    window_sec: float = 60.0
    throttle_fraction: float = 0.85
    pause_sec: float = 4.0
    token_count_mode: str = "output"
    rpm_limit: int | None = None
    rpm_window_sec: float = 60.0
    max_concurrency: int = 1
    shared_scope: str | None = None
    enabled: bool = True

    _window: RollingTokenWindow = field(init=False, repr=False)
    _request_window: RollingTokenWindow = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)
    _semaphore: threading.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.tpm_limit <= 0:
            raise ValueError("RollingTPMPacer: tpm_limit must be positive")
        if not (0.0 < self.throttle_fraction <= 1.0):
            raise ValueError("RollingTPMPacer: throttle_fraction must be in (0, 1]")
        if self.pause_sec < 0:
            raise ValueError("RollingTPMPacer: pause_sec must be >= 0")
        if self.rpm_limit is not None and self.rpm_limit <= 0:
            raise ValueError("RollingTPMPacer: rpm_limit must be positive when set")
        if self.rpm_window_sec <= 0:
            raise ValueError("RollingTPMPacer: rpm_window_sec must be > 0")
        if self.max_concurrency <= 0:
            raise ValueError("RollingTPMPacer: max_concurrency must be positive")
        if self.token_count_mode not in {"total", "output"}:
            raise ValueError("RollingTPMPacer: token_count_mode must be 'total' or 'output'")
        scope = self.shared_scope
        if scope:
            self._window = _shared_window(_SHARED_TOKEN_WINDOWS, scope, self.window_sec)
            self._request_window = _shared_window(_SHARED_REQUEST_WINDOWS, scope, self.rpm_window_sec)
            self._lock = _shared_scope_lock(scope)
            self._semaphore = _shared_semaphore(scope, self.max_concurrency)
        else:
            self._window = RollingTokenWindow(window_sec=self.window_sec)
            self._request_window = RollingTokenWindow(window_sec=self.rpm_window_sec)
            self._lock = threading.Lock()
            self._semaphore = threading.Semaphore(self.max_concurrency)

    # ------------------------------------------------------------------
    # ModelClient protocol surface
    # ------------------------------------------------------------------

    @property
    def route(self) -> dict[str, Any]:
        return self.client.route  # type: ignore[no-any-return]

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        """Pace, delegate, record, return."""
        if self.enabled:
            with self._semaphore:
                self._maybe_pause()
                result = self.client.complete(messages, **kwargs)
        else:
            result = self.client.complete(messages, **kwargs)

        if self.enabled:
            tokens = _extract_metered_tokens(result, mode=self.token_count_mode)
            if tokens > 0:
                now = time.monotonic()
                with self._lock:
                    self._window.record(tokens, ts=now)
                    self._request_window.record(1, ts=now)
                    rolling = self._window.rolling_total(ts=now)
                logger.debug(
                    "tpm_pacer: recorded %d %s tokens; rolling %.0f s total=%d / limit=%d",
                    tokens,
                    self.token_count_mode,
                    self.window_sec,
                    rolling,
                    self.tpm_limit,
                )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_pause(self) -> None:
        """Sleep until shared token/request windows are back under threshold."""
        threshold = int(self.tpm_limit * self.throttle_fraction)
        sleep_for = 0.0
        with self._lock:
            now = time.monotonic()
            rolling = self._window.rolling_total(ts=now)
            if rolling >= threshold:
                sleep_for = max(sleep_for, self._seconds_until_below(self._window, now))
            if self.rpm_limit is not None:
                request_threshold = max(1, int(self.rpm_limit * self.throttle_fraction))
                request_rolling = self._request_window.rolling_total(ts=now)
                if request_rolling >= request_threshold:
                    sleep_for = max(sleep_for, self._seconds_until_below(self._request_window, now))
        if sleep_for > 0:
            sleep_for = max(float(self.pause_sec), sleep_for)
            logger.info(
                "tpm_pacer: scope=%s over rolling provider budget. "
                "Pausing %.1f s before next request.",
                self.shared_scope,
                sleep_for,
            )
            time.sleep(sleep_for)

    @staticmethod
    def _seconds_until_below(window: RollingTokenWindow, now: float) -> float:
        if not window._events:
            return 0.0
        oldest = window._events[0].timestamp
        return max(0.0, (oldest + window.window_sec) - now + 0.1)

    # ------------------------------------------------------------------
    # Inspection helpers (useful for tests / diagnostics)
    # ------------------------------------------------------------------

    def rolling_total(self) -> int:
        """Return the current rolling token total (for diagnostics / tests)."""
        return self._window.rolling_total()

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of pacer state."""
        rolling = self._window.rolling_total()
        threshold = int(self.tpm_limit * self.throttle_fraction)
        return {
            "enabled": self.enabled,
            "tpm_limit": self.tpm_limit,
            "window_sec": self.window_sec,
            "throttle_fraction": self.throttle_fraction,
            "pause_sec": self.pause_sec,
            "token_count_mode": self.token_count_mode,
            "rpm_limit": self.rpm_limit,
            "rpm_window_sec": self.rpm_window_sec,
            "shared_scope": self.shared_scope,
            "rolling_total": rolling,
            "threshold": threshold,
            "headroom": max(0, threshold - rolling),
            "is_throttled": rolling >= threshold,
        }


# ---------------------------------------------------------------------------
# Token extraction helpers
# ---------------------------------------------------------------------------

def _extract_output_tokens(result: dict[str, Any]) -> int:
    """Extract the output/completion token count from a normalised completion result.

    The harness uses both ``output_tokens`` (Responses API / Codex) and
    ``completion_tokens`` (Chat Completions API) in the usage dict.  We try
    both keys so the pacer works across all provider routes.
    """
    if not isinstance(result, dict):
        return 0
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return 0
    for key in ("output_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and value > 0:
            return value
    # Fallback: total_tokens minus prompt_tokens when output key is absent
    total = usage.get("total_tokens")
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    if isinstance(total, int) and isinstance(prompt, int) and total > prompt:
        return total - prompt
    return 0


def _extract_total_tokens(result: dict[str, Any]) -> int:
    if not isinstance(result, dict):
        return 0
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return 0
    value = usage.get("total_tokens")
    if isinstance(value, int) and value > 0:
        return value
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    output_tokens = _extract_output_tokens(result)
    if isinstance(input_tokens, int) and input_tokens > 0:
        return input_tokens + output_tokens
    return output_tokens


def _extract_metered_tokens(result: dict[str, Any], *, mode: str) -> int:
    if mode == "output":
        return _extract_output_tokens(result)
    return _extract_total_tokens(result)


def _default_shared_scope(client: Any) -> str:
    route = getattr(client, "route", None)
    if not isinstance(route, dict):
        return "default"
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


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def make_paced_client(
    client: Any,
    *,
    tpm_limit: int = 100_000,
    window_sec: float = 60.0,
    throttle_fraction: float = 0.85,
    pause_sec: float = 4.0,
    token_count_mode: str = "output",
    rpm_limit: int | None = None,
    rpm_window_sec: float = 60.0,
    max_concurrency: int = 1,
    shared_scope: str | None = None,
    enabled: bool = True,
) -> RollingTPMPacer:
    """Wrap *client* in a :class:`RollingTPMPacer` with given settings.

    Callers that want to opt-out of pacing (e.g. no-model stub runs) should
    pass ``enabled=False``; the wrapper is still returned so downstream code
    has a uniform interface.
    """
    return RollingTPMPacer(
        client=client,
        tpm_limit=tpm_limit,
        window_sec=window_sec,
        throttle_fraction=throttle_fraction,
        pause_sec=pause_sec,
        token_count_mode=token_count_mode,
        rpm_limit=rpm_limit,
        rpm_window_sec=rpm_window_sec,
        max_concurrency=max_concurrency,
        shared_scope=shared_scope,
        enabled=enabled,
    )
