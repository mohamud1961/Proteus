"""Generic, provider-agnostic retry-with-backoff and rate limiting.

Extracted from ``azure_model.py`` so the mechanism (how to retry, how to
pace requests) stays independent of the policy (which Azure/openai errors
are transient). Nothing here imports ``openai`` or knows about the Responses
API — it operates on plain callables and exceptions, which is what makes it
possible to unit test with fakes instead of real network/SDK objects.

Context: a 15-agent parallel batch run against one Azure deployment produced
a rate-limit storm (94.6% of verifier model calls failed with
``ResponseError(code='rate_limit_exceeded')``) because there was no backoff
at all — a single 429 was fatal to the calling turn. This module is the
substrate fix: retry TRANSIENT failures with bounded, jittered backoff, and
optionally pace outgoing requests client-side. It never decides whether a
*specific* error is transient — that classification is the caller's job
(see ``is_retryable_azure_error`` in ``azure_model.py``) — so a config or
judgment bug can't hide behind a blind "retry everything."
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Retry defaults (env-var resolution happens in the caller, e.g. azure_model.py,
# so this module stays free of os.environ coupling).
# ---------------------------------------------------------------------------

DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE_S = 2.0
DEFAULT_BACKOFF_CAP_S = 60.0
DEFAULT_BACKOFF_MAX_TOTAL_S = 300.0
DEFAULT_MAX_RPM = 0.0  # <= 0 means "unlimited" (rate limiter disabled).


def compute_backoff_s(
    attempt: int,
    *,
    base_s: float,
    cap_s: float,
    rand: Callable[[], float] = random.random,
) -> float:
    """Full-jitter exponential backoff delay for a 0-indexed retry *attempt*.

    ``attempt=0`` is the delay before the *first* retry (i.e. after the
    initial call already failed once). The ceiling doubles each attempt and
    is capped at *cap_s*; the actual delay is uniform in ``[0, ceiling)``
    ("full jitter", per the AWS backoff writeup) so concurrent callers that
    fail at the same moment don't all wake up and retry in lockstep — which
    is exactly the failure shape a rate-limit storm produces.
    """
    ceiling = min(cap_s, base_s * (2**attempt))
    return rand() * ceiling


def _retry_call(
    fn: Callable[[], T],
    *,
    is_retryable: Callable[[BaseException], bool],
    max_retries: int,
    base_s: float,
    sleep: Callable[[float], None] = time.sleep,
    cap_s: float = DEFAULT_BACKOFF_CAP_S,
    max_total_s: float | None = DEFAULT_BACKOFF_MAX_TOTAL_S,
    rand: Callable[[], float] = random.random,
) -> T:
    """Call ``fn()`` (zero-arg), retrying on transient failure with backoff.

    ``fn`` is retried in place — each attempt is a fresh, independent call
    (no partial state carries over), which matches the Azure background-job
    case where a failed job can't be resumed and a retry means starting a
    new one.

    Retries happen only when ``is_retryable(exc)`` is True *and* the retry
    budget is not exhausted. The budget is bounded three ways: attempt count
    (``max_retries``), the cap on any single sleep (``cap_s``), and the cap
    on cumulative sleep across all attempts (``max_total_s``, or ``None``
    for no total cap). Non-retryable exceptions propagate on the very first
    attempt with no sleep at all.

    On exhaustion, the *last* exception raised by ``fn`` propagates
    unchanged — this helper never wraps or rebrands errors, so callers see
    the same exception type they'd see without retry (e.g. an
    ``AzureModelError`` stays an ``AzureModelError``).
    """
    attempt = 0
    total_slept = 0.0
    while True:
        try:
            return fn()
        except Exception as exc:
            if not is_retryable(exc) or attempt >= max_retries:
                raise
            delay = compute_backoff_s(attempt, base_s=base_s, cap_s=cap_s, rand=rand)
            if max_total_s is not None and total_slept + delay > max_total_s:
                raise
            sleep(delay)
            total_slept += delay
            attempt += 1


# ---------------------------------------------------------------------------
# Optional client-side rate limiter (min-interval gate a la token bucket).
# ---------------------------------------------------------------------------


class RateLimiter:
    """Thread-safe requests-per-minute gate.

    Enforces a minimum spacing of ``60 / rpm`` seconds between the starts of
    successive ``acquire()`` calls, scheduled forward (not compounding) so a
    burst of callers gets spread evenly rather than serialized behind a
    single growing queue. With ``rpm <= 0`` the limiter is a permanent no-op
    — this is how "unlimited/off" is expressed, so a caller that never sets
    ``AETHER_MODEL_MAX_RPM`` sees no behavior change at all.

    Only the bookkeeping (computing *when* this caller may proceed) happens
    under the lock; the actual sleep happens outside it, so callers waiting
    on different scheduled slots can sleep concurrently instead of queuing
    one at a time behind the lock.
    """

    def __init__(
        self,
        rpm: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rpm = rpm
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._min_interval_s = 60.0 / rpm if rpm > 0 else 0.0
        self._next_allowed_at: float | None = None

    def acquire(self) -> None:
        """Block (via the injected sleep) until this caller's slot arrives."""
        if self._rpm <= 0:
            return
        with self._lock:
            now = self._clock()
            base = self._next_allowed_at if self._next_allowed_at is not None else now
            wait_s = max(0.0, base - now)
            self._next_allowed_at = max(now, base) + self._min_interval_s
        if wait_s > 0:
            self._sleep(wait_s)


_registry_lock = threading.Lock()
_registry: dict[str, RateLimiter] = {}


def get_rate_limiter_for_deployment(deployment: str, rpm: float) -> RateLimiter:
    """Return a process-wide ``RateLimiter`` shared by all callables for *deployment*.

    Multiple ``AzureModelCallable`` instances built for the same deployment
    name (e.g. solver + verifier + architect all pointed at one Azure
    deployment, as in the batch run that motivated this module) share one
    bucket, so the configured RPM is a true per-deployment ceiling rather
    than a per-callable one that under-counts concurrent callers.

    First call for a given *deployment* wins on the *rpm* value; later calls
    reuse the existing limiter rather than resetting its schedule.
    """
    with _registry_lock:
        limiter = _registry.get(deployment)
        if limiter is None:
            limiter = RateLimiter(rpm)
            _registry[deployment] = limiter
        return limiter
