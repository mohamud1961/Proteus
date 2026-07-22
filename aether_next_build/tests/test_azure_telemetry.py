"""No-network tests for Azure Responses cache layout and telemetry."""
from __future__ import annotations

import json
from types import SimpleNamespace
import pytest

from aether_next.model_hooks import ModelHooks
from aether_next.providers.azure_model import (
    AzureModelCallable,
    AzureModelError,
    AzureVisionCallable,
)
from aether_next.runners.docker_runner import KernelRunTimeout
from aether_next.run_adapter import workbench_architect_for


class _Job:
    def __init__(self, *, usage=None, status="completed", job_id="job-telemetry", error=None) -> None:
        self.id = job_id
        self.status = status
        self.output_text = "AGGREGATED-NON-AUTHORITY"
        self.output = [
            SimpleNamespace(
                id=f"{job_id}-message",
                type="message",
                role="assistant",
                content=[SimpleNamespace(type="output_text", text="{}")],
            )
        ]
        self.error = error
        self.incomplete_details = None
        self.usage = usage


class _Responses:
    def __init__(self, outer: "_Client") -> None:
        self._outer = outer

    def create(self, **kwargs):
        self._outer.requests.append(kwargs)
        if self._outer.require_json_in_input and "json" not in json.dumps(
            kwargs["input"], sort_keys=True
        ).lower():
            raise RuntimeError(
                "Response input messages must contain the word 'json' in some form "
                "to use 'text.format' of type 'json_object'."
            )
        effect = self._outer.effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def retrieve(self, job_id):  # pragma: no cover - terminal jobs only
        raise AssertionError(job_id)


class _Client:
    def __init__(self, effects, *, require_json_in_input: bool = False) -> None:
        self.effects = list(effects)
        self.requests: list[dict] = []
        self.require_json_in_input = require_json_in_input
        self.responses = _Responses(self)


def _model(client: _Client, *, deployment="gpt-5.4-mini", role="solver", **kwargs) -> AzureModelCallable:
    max_retries = kwargs.pop("max_retries", 0)
    sleep = kwargs.pop("sleep", lambda _: None)
    return AzureModelCallable(
        client=client,  # type: ignore[arg-type]
        deployment=deployment,
        effort="medium",
        role=role,
        poll_interval_s=1,
        poll_timeout_s=30,
        max_retries=max_retries,
        sleep=sleep,
        **kwargs,
    )


def test_operator_cache_shard_ignores_task_suffix_and_retains_reported_usage() -> None:
    usage = {
        "input_tokens": 4096,
        "output_tokens": 120,
        "total_tokens": 4216,
        "input_tokens_details": {"cached_tokens": 3072},
        "output_tokens_details": {"reasoning_tokens": 85},
    }
    client = _Client([_Job(usage=usage), _Job(usage=usage)])
    model = _model(client)

    prefix = "immutable common protocol\nTASK-SPECIFIC-SUFFIX-A"
    assert model([
        {"role": "system", "content": prefix},
        {"role": "system", "content": "[context_packet] first"},
    ]) == "{}"
    assert model([
        {"role": "system", "content": "immutable common protocol\nTASK-SPECIFIC-SUFFIX-B"},
        {"role": "system", "content": "[context_packet] second"},
    ]) == "{}"

    first, second = client.requests
    assert first["instructions"].startswith(prefix)
    assert "return exactly one valid json object" in first["instructions"].lower()
    assert first["input"] != second["input"]
    assert isinstance(first["input"], list)
    assert first["input"][0]["role"] == "developer"
    assert "json" in first["input"][0]["content"].lower()
    assert first["input"][1]["role"] == "user"
    assert first["text"] == {"format": {"type": "json_object"}}
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert len(first["prompt_cache_key"]) <= 64
    assert "context_packet" not in first["prompt_cache_key"]
    assert "TASK-SPECIFIC" not in first["prompt_cache_key"]
    assert first["prompt_cache_retention"] == "in_memory"

    events = model.drain_telemetry()
    assert len(events) == 2
    assert events[0]["usage_status"] == "reported"
    assert events[0]["cache_metrics_status"] == "reported"
    assert events[0]["cached_input_tokens"] == 3072
    assert events[0]["logical_call_id"] != events[1]["logical_call_id"]
    assert events[0]["attempt_ordinal"] == 1
    assert events[0]["instructions_sha256"]


def test_json_mode_adds_an_explicit_instruction_for_all_system_solver_messages() -> None:
    """Reproduce the role-board shape Azure rejected before generation.

    Solver checkpoint calls contain only system messages.  The Responses
    adapter promotes the last one to input, so neither side may rely on a
    task's incidental wording to satisfy Azure's JSON-mode requirement.
    """
    client = _Client([_Job()])
    model = _model(client)

    assert model([
        {"role": "system", "content": "Choose one causal next action."},
        {"role": "system", "content": "Current evidence is incomplete."},
    ]) == "{}"

    request = client.requests[0]
    assert request["text"] == {"format": {"type": "json_object"}}
    assert "json" in request["instructions"].lower()
    assert "return exactly one valid json object" in request["instructions"].lower()
    assert "json" in json.dumps(request["input"], sort_keys=True).lower()


def test_json_mode_survives_azure_input_message_requirement() -> None:
    """Reproduce the live Azure route that ignored ``instructions`` alone."""
    client = _Client([_Job()], require_json_in_input=True)
    model = _model(client)

    assert model([
        {"role": "system", "content": "Choose one causal next action."},
        {"role": "system", "content": "Current evidence is incomplete."},
    ]) == "{}"

    request = client.requests[0]
    assert "json" in json.dumps(request["input"], sort_keys=True).lower()


def test_json_mode_contract_is_identical_across_a_bounded_retry() -> None:
    client = _Client([
        _Job(status="failed", error=SimpleNamespace(code="rate_limit_exceeded")),
        _Job(),
    ])
    model = _model(client, max_retries=1, rand=lambda: 0.0)

    assert model([{"role": "system", "content": "No structured keyword here."}]) == "{}"

    first, second = client.requests
    assert first["instructions"] == second["instructions"]
    assert "return exactly one valid json object" in first["instructions"].lower()
    assert first["input"] == second["input"]
    assert "json" in json.dumps(first["input"], sort_keys=True).lower()


def test_omitted_usage_is_unmeasured_not_a_zero_cache_miss() -> None:
    model = _model(_Client([_Job(usage=None)]))
    model([{"role": "user", "content": "hello"}])

    event = model.drain_telemetry()[0]
    assert event["usage_status"] == "omitted"
    assert event["cache_metrics_status"] == "unmeasured"
    assert event["cached_input_tokens"] is None
    assert event["input_tokens"] is None


def test_failed_request_is_retained_in_telemetry() -> None:
    model = _model(_Client([RuntimeError("no route")]))

    with pytest.raises(AzureModelError):
        model([{"role": "user", "content": "hello"}])

    event = model.drain_telemetry()[0]
    assert event["status"] == "failed"
    assert event["attempt_ordinal"] == 1
    assert event["cache_metrics_status"] == "unmeasured"


def test_cache_shard_changes_for_deployment_role_or_namespace() -> None:
    message = [
        {"role": "system", "content": "common protocol\nunique task suffix"},
        {"role": "system", "content": "[context_packet] dynamic"},
    ]
    solver = _model(_Client([_Job()]), role="solver")
    architect = _model(_Client([_Job()]), role="architect")
    other_deployment = _model(_Client([_Job()]), deployment="other-mini", role="solver")
    other_namespace = _model(
        _Client([_Job()]), role="solver", prompt_cache_namespace="aether-next-v2",
    )

    solver(message)
    architect(message)
    other_deployment(message)
    other_namespace(message)
    keys = {
        solver._client.requests[0]["prompt_cache_key"],  # type: ignore[attr-defined]
        architect._client.requests[0]["prompt_cache_key"],  # type: ignore[attr-defined]
        other_deployment._client.requests[0]["prompt_cache_key"],  # type: ignore[attr-defined]
        other_namespace._client.requests[0]["prompt_cache_key"],  # type: ignore[attr-defined]
    }
    assert len(keys) == 4


class _JobError:
    def __init__(self, code: str) -> None:
        self.code = code


def test_retries_preserve_one_telemetry_row_per_provider_attempt() -> None:
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    client = _Client([
        _Job(status="failed", job_id="job-rate-limit", error=_JobError("rate_limit_exceeded")),
        _Job(status="completed", job_id="job-success", usage=usage),
    ])
    model = _model(client, max_retries=1, rand=lambda: 0.0)

    assert model([{"role": "user", "content": "retry once"}]) == "{}"
    first, second = model.drain_telemetry()
    assert first["logical_call_id"] == second["logical_call_id"]
    assert [first["attempt_ordinal"], second["attempt_ordinal"]] == [1, 2]
    assert first["job_id"] == "job-rate-limit"
    assert first["job_status"] == "failed"
    assert first["status"] == "failed"
    assert second["job_id"] == "job-success"
    assert second["status"] == "completed"
    assert second["input_tokens"] == 100


def test_vision_call_retains_usage_without_a_cache_claim() -> None:
    vision = AzureVisionCallable(
        _Client([_Job(usage={"input_tokens": 77, "output_tokens": 11, "total_tokens": 88})]),
        "gpt-5.4-mini",
    )

    assert vision("describe image", "ZmFrZQ==", "image/png") == "{}"

    event = vision.drain_telemetry()[0]
    assert event["provider"] == "azure_openai_responses_vision"
    assert event["status"] == "completed"
    assert event["usage_status"] == "reported"
    assert event["cached_input_tokens"] is None
    assert event["prompt_cache_key_mode"] == "not_requested"


def test_kernel_timeout_passthrough_still_records_attempt() -> None:
    model = _model(_Client([KernelRunTimeout("kernel wall clock")]))

    with pytest.raises(KernelRunTimeout):
        model([{"role": "user", "content": "interrupted"}])

    event = model.drain_telemetry()[0]
    assert event["status"] == "failed"
    assert event["error_type"] == "KernelRunTimeout"
    assert event["attempt_phase"] == "create"


def test_late_event_is_quarantined_at_the_next_task_boundary() -> None:
    client = _Client([_Job(job_id="old-late"), _Job(job_id="new-current")])
    provider = _model(client)
    old_hooks = ModelHooks(provider, provider, run_id="run-old", task_id="task-old")
    new_hooks = ModelHooks(provider, provider, run_id="run-new", task_id="task-new")

    # Simulate an uncancelled old verifier finishing after the next task has
    # already created its hooks, then make one current-task call.
    old_hooks._call_text_model(provider, [{"role": "user", "content": "late old"}], max_output_tokens=8)
    new_hooks._call_text_model(provider, [{"role": "user", "content": "new task"}], max_output_tokens=8)

    current = new_hooks.drain_model_telemetry()
    quarantined = new_hooks.drain_quarantined_model_telemetry()
    assert [row["run_id"] for row in current] == ["run-new"]
    assert [row["task_id"] for row in current] == ["task-new"]
    assert [row["run_id"] for row in quarantined] == ["run-old"]
    assert quarantined[0]["task_id"] == "task-old"
    assert quarantined[0]["telemetry_quarantine_reason"] == (
        "late_or_unscoped_event_not_owned_by_current_run"
    )


def test_workbench_architect_uses_current_run_scope_and_quarantines_late_old_event() -> None:
    client = _Client([_Job(job_id="old-architect"), _Job(job_id="new-architect")])
    provider = _model(client)
    old_hooks = ModelHooks(provider, provider, run_id="run-old", task_id="task-old")
    new_hooks = ModelHooks(provider, provider, run_id="run-new", task_id="task-new")

    old_hooks.call_architect_model(
        [{"role": "user", "content": "late old architect"}], max_output_tokens=8,
    )
    architect = workbench_architect_for(provider, hooks=new_hooks)
    architect._call_model(
        [{"role": "user", "content": "current architect"}], max_output_tokens=8,
    )

    current = new_hooks.drain_model_telemetry()
    quarantined = new_hooks.drain_quarantined_model_telemetry()
    assert [row["run_id"] for row in current] == ["run-new"]
    assert [row["task_id"] for row in current] == ["task-new"]
    assert [row["run_id"] for row in quarantined] == ["run-old"]
    assert quarantined[0]["task_id"] == "task-old"
