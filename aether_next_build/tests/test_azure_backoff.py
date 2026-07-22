"""Tests for adaptive retry/backoff + client-side rate limiting on Azure
model calls — the substrate fix for the 15-agent parallel batch rate-limit
storm documented in FABLE5_BATCH_AUDIT_20260709T101515Z.md (94.6% of
verifier model calls and hundreds of solver calls per task failed with
``ResponseError(code='rate_limit_exceeded')`` / "background job … ended
with status=failed", with no backoff at all to absorb it).

NO real network calls anywhere in this file: every HTTP/SDK interaction is
a local fake (``_FakeClient``, ``_FakeHTTPError``, ``_FakeJob``), and every
sleep/clock/jitter source is injected so the whole suite runs in a fraction
of a second with zero flakiness. Real ``openai`` exception *types* are
constructed in one section (no I/O — just object construction) to prove the
duck-typed classifier also works against the actual SDK, not only against
local stand-ins.
"""
from __future__ import annotations

from types import SimpleNamespace
import httpx
import openai
import pytest

from aether_next.providers.azure_model import (
    AzureModelCallable,
    AzureModelError,
    _JobStatusFailure,
    is_retryable_azure_error,
    make_azure_callable,
)
from aether_next.providers.model_retry import (
    DEFAULT_BACKOFF_BASE_S,
    DEFAULT_BACKOFF_CAP_S,
    DEFAULT_BACKOFF_MAX_TOTAL_S,
    DEFAULT_MAX_RETRIES,
    RateLimiter,
    _retry_call,
    compute_backoff_s,
    get_rate_limiter_for_deployment,
)


# ---------------------------------------------------------------------------
# Test doubles — no network, no real SDK transport.
# ---------------------------------------------------------------------------


class _FakeHTTPError(Exception):
    """Duck-typed stand-in for an openai.APIStatusError subclass.

    Real openai errors expose ``.status_code`` (verified against the
    installed SDK: RateLimitError.status_code == 429,
    InternalServerError.status_code == 500, BadRequestError.status_code ==
    400, …) — this fake carries the same attribute so the classifier under
    test sees an identical shape without needing an httpx.Response.
    """

    def __init__(self, status_code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.status_code = status_code


class _FakeJobError:
    """Stands in for an openai ``ResponseError`` (``job.error``)."""

    def __init__(self, code: str | None, message: str = "boom") -> None:
        self.code = code
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - cosmetic only
        return f"ResponseError(code={self.code!r}, message={self.message!r})"


class _FakeJob:
    """Stands in for a Responses API background job object."""

    def __init__(
        self,
        *,
        status: str,
        job_id: str = "job-1",
        output_text: str = "",
        error: _FakeJobError | None = None,
    ) -> None:
        self.id = job_id
        self.status = status
        # Aggregated output_text is retained only to prove the provider does
        # not use it as authority.  The canonical message lives in raw items.
        self.output_text = output_text
        self.output = ([
            SimpleNamespace(
                id=f"{job_id}-message",
                type="message",
                role="assistant",
                content=[SimpleNamespace(type="output_text", text=output_text)],
            )
        ] if output_text else [])
        self.error = error
        self.incomplete_details = None


class _RecordingSleep:
    """Fake sleep: records durations instead of blocking."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FakeResponses:
    def __init__(self, outer: "_FakeClient") -> None:
        self._outer = outer

    def create(self, **kwargs: object) -> _FakeJob:
        outer = self._outer
        idx = min(outer.create_calls, len(outer._effects) - 1)
        effect = outer._effects[idx]
        outer.create_calls += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def retrieve(self, job_id: str) -> _FakeJob:  # pragma: no cover - unused by these tests
        raise AssertionError(
            "retrieve() should not be called: every fake job in this suite "
            "resolves to a terminal status directly from create()"
        )


class _FakeClient:
    """Duck-typed stand-in for openai.OpenAI's ``.responses`` surface.

    ``create_effects`` is consumed in order, one per call to ``.create()``:
    an exception instance is raised, a ``_FakeJob`` is returned. The last
    entry repeats once the list is exhausted, so "always fails" only needs
    a single-element list.
    """

    def __init__(self, create_effects: list) -> None:
        self._effects = list(create_effects)
        self.create_calls = 0

    @property
    def responses(self) -> _FakeResponses:
        return _FakeResponses(self)


def _model(
    client: _FakeClient,
    *,
    sleeper: _RecordingSleep,
    max_retries: int = 3,
    backoff_base_s: float = 0.01,
    backoff_cap_s: float = 1.0,
    backoff_max_total_s: float | None = DEFAULT_BACKOFF_MAX_TOTAL_S,
    rand=lambda: 1.0,
    rate_limiter: RateLimiter | None = None,
) -> AzureModelCallable:
    return AzureModelCallable(
        client=client,  # type: ignore[arg-type]
        deployment="unit-test-deployment",
        effort="medium",
        poll_interval_s=1.0,
        poll_timeout_s=30.0,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
        backoff_max_total_s=backoff_max_total_s,
        rate_limiter=rate_limiter,
        sleep=sleeper,
        rand=rand,
    )


# ---------------------------------------------------------------------------
# (a) _retry_call — retryable error N times then success.
# ---------------------------------------------------------------------------


class TestRetryCallSucceedsAfterTransientFailures:
    def test_retries_then_succeeds_with_growing_backoff(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] <= 3:
                raise _FakeHTTPError(429)
            return "ok"

        sleeper = _RecordingSleep()
        result = _retry_call(
            flaky,
            is_retryable=lambda exc: True,
            max_retries=5,
            base_s=1.0,
            cap_s=60.0,
            sleep=sleeper,
            rand=lambda: 1.0,  # disable jitter: ceiling == delay, deterministic
        )

        assert result == "ok"
        assert calls["n"] == 4, "N=3 failures + 1 success => attempts == N+1"
        assert sleeper.calls == [1.0, 2.0, 4.0], "exponential growth, base 1.0"
        assert all(b > a for a, b in zip(sleeper.calls, sleeper.calls[1:])), "sleeps grew"


# ---------------------------------------------------------------------------
# (b) exhausted retries -> clean, unwrapped exception (AzureModelError at
# the AzureModelCallable layer; whatever fn raises at the pure _retry_call
# layer, since the helper itself is provider-agnostic).
# ---------------------------------------------------------------------------


class TestRetryCallExhaustion:
    def test_exhausted_retries_reraises_last_exception_unchanged(self) -> None:
        def always_fails() -> str:
            raise _FakeHTTPError(429, "still limited")

        sleeper = _RecordingSleep()
        with pytest.raises(_FakeHTTPError, match="still limited"):
            _retry_call(
                always_fails,
                is_retryable=lambda exc: True,
                max_retries=3,
                base_s=0.01,
                cap_s=1.0,
                sleep=sleeper,
                rand=lambda: 1.0,
            )
        assert len(sleeper.calls) == 3, "one sleep per retry, none after the final failure"

    def test_max_total_s_stops_retrying_even_with_attempts_left(self) -> None:
        sleeper = _RecordingSleep()

        def always_429() -> str:
            raise _FakeHTTPError(429)

        with pytest.raises(_FakeHTTPError):
            _retry_call(
                always_429,
                is_retryable=lambda exc: True,
                max_retries=10,
                base_s=10.0,
                cap_s=60.0,
                max_total_s=15.0,
                sleep=sleeper,
                rand=lambda: 1.0,
            )
        # Ceilings (rand=1.0): 10, 20, 40, ... First sleep (10) keeps total
        # at 10 <= 15, so it proceeds. Second would-be sleep (20) would push
        # total to 30 > 15, so it raises instead of sleeping again.
        assert sleeper.calls == [10.0]


class TestAzureModelCallableExhaustionRaisesAzureModelError:
    def test_persistent_429_raises_azure_model_error_after_max_retries(self) -> None:
        client = _FakeClient([_FakeHTTPError(429, "still limited")])
        sleeper = _RecordingSleep()
        model = _model(client, sleeper=sleeper, max_retries=3)

        with pytest.raises(AzureModelError) as excinfo:
            model([{"role": "user", "content": "hi"}])

        assert "responses.create failed" in str(excinfo.value)
        assert client.create_calls == 4, "initial attempt + 3 retries"
        assert len(sleeper.calls) == 3

    def test_persistent_retryable_job_failure_raises_azure_model_error(self) -> None:
        client = _FakeClient(
            [_FakeJob(status="failed", error=_FakeJobError("rate_limit_exceeded"))]
        )
        sleeper = _RecordingSleep()
        model = _model(client, sleeper=sleeper, max_retries=2)

        with pytest.raises(AzureModelError) as excinfo:
            model([{"role": "user", "content": "hi"}])

        assert "status=failed" in str(excinfo.value)
        assert client.create_calls == 3
        assert len(sleeper.calls) == 2


# ---------------------------------------------------------------------------
# (c) non-retryable error -> raises immediately, no retry.
# ---------------------------------------------------------------------------


class TestRetryCallNonRetryable:
    def test_non_retryable_raises_immediately_without_sleep(self) -> None:
        calls = {"n": 0}

        def bad_request() -> str:
            calls["n"] += 1
            raise _FakeHTTPError(400, "bad request")

        sleeper = _RecordingSleep()
        with pytest.raises(_FakeHTTPError):
            _retry_call(
                bad_request,
                is_retryable=lambda exc: False,
                max_retries=5,
                base_s=1.0,
                sleep=sleeper,
            )
        assert calls["n"] == 1
        assert sleeper.calls == []


class TestAzureModelCallableNonRetryableRaisesImmediately:
    def test_400_bad_request_raises_immediately(self) -> None:
        client = _FakeClient([_FakeHTTPError(400, "bad request")])
        sleeper = _RecordingSleep()
        model = _model(client, sleeper=sleeper, max_retries=5)

        with pytest.raises(AzureModelError):
            model([{"role": "user", "content": "hi"}])

        assert client.create_calls == 1
        assert sleeper.calls == []

    def test_job_status_failed_with_non_retryable_code_raises_immediately(self) -> None:
        client = _FakeClient(
            [_FakeJob(status="failed", error=_FakeJobError("invalid_prompt"))]
        )
        sleeper = _RecordingSleep()
        model = _model(client, sleeper=sleeper, max_retries=5)

        with pytest.raises(AzureModelError) as excinfo:
            model([{"role": "user", "content": "hi"}])

        assert "status=failed" in str(excinfo.value)
        assert client.create_calls == 1
        assert sleeper.calls == []


class TestAzureModelCallableRetriesThenSucceeds:
    def test_retries_429_on_create_then_succeeds(self) -> None:
        client = _FakeClient(
            [
                _FakeHTTPError(429, "rate limited"),
                _FakeHTTPError(429, "rate limited"),
                _FakeJob(status="completed", output_text='{"value":"hello"}'),
            ]
        )
        sleeper = _RecordingSleep()
        model = _model(client, sleeper=sleeper, max_retries=5, backoff_base_s=0.01, backoff_cap_s=1.0)

        result = model([{"role": "user", "content": "hi"}])

        assert result == '{"value":"hello"}'
        assert client.create_calls == 3
        assert len(sleeper.calls) == 2
        assert sleeper.calls[1] > sleeper.calls[0], "backoff grew between retries"

    def test_retries_rate_limit_exceeded_job_failure_then_succeeds(self) -> None:
        client = _FakeClient(
            [
                _FakeJob(status="failed", error=_FakeJobError("rate_limit_exceeded")),
                _FakeJob(status="completed", output_text='{"value":"done"}'),
            ]
        )
        sleeper = _RecordingSleep()
        model = _model(client, sleeper=sleeper, max_retries=3)

        result = model([{"role": "user", "content": "hi"}])

        assert result == '{"value":"done"}'
        assert client.create_calls == 2
        assert len(sleeper.calls) == 1


class TestAzureModelCallableUsesRateLimiter:
    def test_acquire_called_once_per_attempt(self) -> None:
        acquire_calls = {"n": 0}

        class _SpyLimiter:
            def acquire(self) -> None:
                acquire_calls["n"] += 1

        client = _FakeClient(
            [_FakeHTTPError(429), _FakeJob(status="completed", output_text='{"value":"ok"}')]
        )
        sleeper = _RecordingSleep()
        model = _model(client, sleeper=sleeper, max_retries=3, rate_limiter=_SpyLimiter())

        result = model([{"role": "user", "content": "hi"}])

        assert result == '{"value":"ok"}'
        assert acquire_calls["n"] == 2, "one acquire() per create() attempt"


# ---------------------------------------------------------------------------
# (d) rate limiter min-interval enforced with a fake clock.
# ---------------------------------------------------------------------------


class TestRateLimiterMinInterval:
    def test_enforces_min_interval_with_fake_clock(self) -> None:
        clock_state = {"t": 0.0}
        sleep_log: list[float] = []

        def fake_clock() -> float:
            return clock_state["t"]

        def fake_sleep(seconds: float) -> None:
            sleep_log.append(seconds)
            clock_state["t"] += seconds  # simulate time passing while "asleep"

        limiter = RateLimiter(rpm=60, clock=fake_clock, sleep=fake_sleep)  # 1 req/sec

        limiter.acquire()  # first call: no prior schedule, proceeds immediately
        assert sleep_log == []

        limiter.acquire()  # second call at the same instant: must wait ~1s
        assert sleep_log == [1.0]

        limiter.acquire()  # third call, fake clock now at t=1.0 after the sleep
        assert sleep_log == [1.0, 1.0]

    def test_callers_spaced_out_naturally_incur_no_wait(self) -> None:
        clock_state = {"t": 0.0}
        sleep_log: list[float] = []
        limiter = RateLimiter(
            rpm=60,
            clock=lambda: clock_state["t"],
            sleep=lambda s: sleep_log.append(s),
        )

        limiter.acquire()  # t=0
        clock_state["t"] = 5.0  # caller arrives well after the min interval
        limiter.acquire()

        assert sleep_log == [], "arrivals already spaced beyond min-interval never wait"


class TestRateLimiterDisabledByDefault:
    def test_zero_rpm_never_sleeps(self) -> None:
        sleep_log: list[float] = []
        limiter = RateLimiter(rpm=0, clock=lambda: 0.0, sleep=lambda s: sleep_log.append(s))
        for _ in range(5):
            limiter.acquire()
        assert sleep_log == []

    def test_negative_rpm_never_sleeps(self) -> None:
        sleep_log: list[float] = []
        limiter = RateLimiter(rpm=-1, clock=lambda: 0.0, sleep=lambda s: sleep_log.append(s))
        limiter.acquire()
        assert sleep_log == []


class TestRateLimiterRegistrySharesPerDeployment:
    def test_same_deployment_name_shares_one_instance(self) -> None:
        a = get_rate_limiter_for_deployment("unit-test-deploy-shared-x", 30)
        b = get_rate_limiter_for_deployment("unit-test-deploy-shared-x", 30)
        assert a is b

    def test_different_deployment_names_get_independent_instances(self) -> None:
        a = get_rate_limiter_for_deployment("unit-test-deploy-indep-y1", 30)
        b = get_rate_limiter_for_deployment("unit-test-deploy-indep-y2", 30)
        assert a is not b


# ---------------------------------------------------------------------------
# compute_backoff_s — exponential growth + cap, in isolation.
# ---------------------------------------------------------------------------


class TestComputeBackoffS:
    def test_grows_exponentially_until_capped(self) -> None:
        no_jitter = lambda: 1.0  # noqa: E731 - local test helper
        delays = [
            compute_backoff_s(attempt, base_s=2.0, cap_s=20.0, rand=no_jitter)
            for attempt in range(6)
        ]
        assert delays == [2.0, 4.0, 8.0, 16.0, 20.0, 20.0]

    def test_jitter_stays_within_ceiling(self) -> None:
        # rand() in [0, 1) always -> full-jitter delay in [0, ceiling).
        for r in (0.0, 0.25, 0.5, 0.75, 0.999):
            delay = compute_backoff_s(3, base_s=1.0, cap_s=100.0, rand=lambda: r)
            assert 0.0 <= delay < 8.0  # ceiling = min(100, 1*2**3) = 8


# ---------------------------------------------------------------------------
# is_retryable_azure_error — HTTP-status duck typing against local fakes.
# ---------------------------------------------------------------------------


class TestIsRetryableAzureErrorHTTPStatus:
    def test_429_is_retryable(self) -> None:
        exc = AzureModelError("responses.create failed: boom")
        exc.__cause__ = _FakeHTTPError(429)
        assert is_retryable_azure_error(exc) is True

    def test_500_is_retryable(self) -> None:
        exc = AzureModelError("boom")
        exc.__cause__ = _FakeHTTPError(500)
        assert is_retryable_azure_error(exc) is True

    def test_599_is_retryable_and_600_is_not(self) -> None:
        exc_a = AzureModelError("boom")
        exc_a.__cause__ = _FakeHTTPError(599)
        assert is_retryable_azure_error(exc_a) is True

        exc_b = AzureModelError("boom")
        exc_b.__cause__ = _FakeHTTPError(600)
        assert is_retryable_azure_error(exc_b) is False

    def test_400_bad_request_is_not_retryable(self) -> None:
        exc = AzureModelError("bad request")
        exc.__cause__ = _FakeHTTPError(400)
        assert is_retryable_azure_error(exc) is False

    def test_401_auth_is_not_retryable(self) -> None:
        exc = AzureModelError("auth failed")
        exc.__cause__ = _FakeHTTPError(401)
        assert is_retryable_azure_error(exc) is False

    def test_403_permission_denied_is_not_retryable(self) -> None:
        exc = AzureModelError("forbidden")
        exc.__cause__ = _FakeHTTPError(403)
        assert is_retryable_azure_error(exc) is False


class TestIsRetryableAzureErrorRealOpenAIExceptions:
    """Cross-check the duck-typed classifier against real SDK exception
    *types* — object construction only, no network I/O — so the classifier
    isn't validated only against local stand-ins."""

    @staticmethod
    def _response(status_code: int) -> httpx.Response:
        request = httpx.Request("POST", "https://example.test/openai/v1/responses")
        return httpx.Response(status_code, request=request, json={"error": {"message": "x"}})

    def test_real_rate_limit_error_is_retryable(self) -> None:
        cause = openai.RateLimitError("rate limited", response=self._response(429), body=None)
        exc = AzureModelError("responses.create failed: rate limited")
        exc.__cause__ = cause
        assert is_retryable_azure_error(exc) is True

    def test_real_internal_server_error_is_retryable(self) -> None:
        cause = openai.InternalServerError("boom", response=self._response(500), body=None)
        exc = AzureModelError("boom")
        exc.__cause__ = cause
        assert is_retryable_azure_error(exc) is True

    def test_real_bad_request_error_is_not_retryable(self) -> None:
        cause = openai.BadRequestError("bad", response=self._response(400), body=None)
        exc = AzureModelError("bad")
        exc.__cause__ = cause
        assert is_retryable_azure_error(exc) is False

    def test_real_authentication_error_is_not_retryable(self) -> None:
        cause = openai.AuthenticationError("nope", response=self._response(401), body=None)
        exc = AzureModelError("nope")
        exc.__cause__ = cause
        assert is_retryable_azure_error(exc) is False

    def test_real_api_connection_error_is_retryable(self) -> None:
        request = httpx.Request("POST", "https://example.test/openai/v1/responses")
        cause = openai.APIConnectionError(request=request)
        exc = AzureModelError("responses.create failed: connection reset")
        exc.__cause__ = cause
        assert is_retryable_azure_error(exc) is True


class TestIsRetryableAzureErrorJobStatusFailure:
    def test_rate_limit_exceeded_job_code_is_retryable(self) -> None:
        exc = AzureModelError("background job job-1 ended with status=failed: boom")
        exc.__cause__ = _JobStatusFailure("rate_limit_exceeded")
        assert is_retryable_azure_error(exc) is True

    def test_server_error_job_code_is_retryable(self) -> None:
        exc = AzureModelError("boom")
        exc.__cause__ = _JobStatusFailure("server_error")
        assert is_retryable_azure_error(exc) is True

    def test_invalid_prompt_job_code_is_not_retryable(self) -> None:
        exc = AzureModelError("boom")
        exc.__cause__ = _JobStatusFailure("invalid_prompt")
        assert is_retryable_azure_error(exc) is False

    def test_unknown_job_code_is_not_retryable(self) -> None:
        exc = AzureModelError("boom")
        exc.__cause__ = _JobStatusFailure(None)
        assert is_retryable_azure_error(exc) is False


class TestIsRetryableAzureErrorNoCause:
    def test_plain_exception_with_no_cause_and_no_status_code_is_not_retryable(self) -> None:
        exc = AzureModelError("responses.create returned no job id")
        assert is_retryable_azure_error(exc) is False


# ---------------------------------------------------------------------------
# make_azure_callable — env-var wiring (build-time), no network calls.
# ---------------------------------------------------------------------------


class TestMakeAzureCallableReadsRetryEnvVars:
    def test_env_vars_configure_retry_and_rpm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UNIT_TEST_AZURE_ENDPOINT", "https://example.test/openai/responses")
        monkeypatch.setenv("UNIT_TEST_AZURE_DEPLOYMENT", "unit-test-env-var-deploy")
        monkeypatch.setenv("UNIT_TEST_AZURE_KEY", "sk-fake")
        monkeypatch.setenv("AETHER_MODEL_MAX_RETRIES", "9")
        monkeypatch.setenv("AETHER_MODEL_BACKOFF_BASE_S", "3.5")
        monkeypatch.setenv("AETHER_MODEL_BACKOFF_CAP_S", "45")
        monkeypatch.setenv("AETHER_MODEL_BACKOFF_MAX_TOTAL_S", "200")
        monkeypatch.setenv("AETHER_MODEL_MAX_RPM", "120")

        model = make_azure_callable(
            deployment_env="UNIT_TEST_AZURE_DEPLOYMENT",
            key_env="UNIT_TEST_AZURE_KEY",
            endpoint_env="UNIT_TEST_AZURE_ENDPOINT",
        )

        assert model._max_retries == 9
        assert model._backoff_base_s == 3.5
        assert model._backoff_cap_s == 45.0
        assert model._backoff_max_total_s == 200.0
        assert model._rate_limiter is get_rate_limiter_for_deployment(
            "unit-test-env-var-deploy", 120
        )

    def test_defaults_when_env_vars_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UNIT_TEST_AZURE_ENDPOINT2", "https://example.test/openai/responses")
        monkeypatch.setenv("UNIT_TEST_AZURE_DEPLOYMENT2", "unit-test-env-var-deploy-defaults")
        monkeypatch.setenv("UNIT_TEST_AZURE_KEY2", "sk-fake")
        for var in (
            "AETHER_MODEL_MAX_RETRIES",
            "AETHER_MODEL_BACKOFF_BASE_S",
            "AETHER_MODEL_BACKOFF_CAP_S",
            "AETHER_MODEL_BACKOFF_MAX_TOTAL_S",
            "AETHER_MODEL_MAX_RPM",
        ):
            monkeypatch.delenv(var, raising=False)

        model = make_azure_callable(
            deployment_env="UNIT_TEST_AZURE_DEPLOYMENT2",
            key_env="UNIT_TEST_AZURE_KEY2",
            endpoint_env="UNIT_TEST_AZURE_ENDPOINT2",
        )

        assert model._max_retries == DEFAULT_MAX_RETRIES
        assert model._backoff_base_s == DEFAULT_BACKOFF_BASE_S
        assert model._backoff_cap_s == DEFAULT_BACKOFF_CAP_S
        assert model._backoff_max_total_s == DEFAULT_BACKOFF_MAX_TOTAL_S
