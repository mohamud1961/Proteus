"""No-network adversarial tests for the structured provider boundary."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aether_next.providers.azure_model import (
    AzureModelCallable,
    AzureProviderOutputError,
    canonicalize_structured_output,
)


def _message(text: str, *, item_id: str = "msg-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id,
        type="message",
        role="assistant",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _response(*items: object, status: str = "completed", output_text: str = "POISON") -> SimpleNamespace:
    return SimpleNamespace(
        id="resp-1",
        status=status,
        output=list(items),
        output_text=output_text,
        usage=None,
        error=None,
        incomplete_details=SimpleNamespace(reason="max_output_tokens") if status == "incomplete" else None,
    )


def test_raw_message_is_authority_not_aggregated_output_text() -> None:
    text, receipt = canonicalize_structured_output(
        _response(_message('{"kind":"submit_outcome","summary":"ready"}'), output_text="not json")
    )

    assert text == '{"kind":"submit_outcome","summary":"ready"}'
    assert receipt["extraction_path"].startswith("response.output[]")


def test_semantically_identical_duplicate_messages_execute_once() -> None:
    text, receipt = canonicalize_structured_output(_response(
        _message('{"a":1,"b":2}', item_id="m1"),
        _message('{\n  "b": 2, "a": 1\n}', item_id="m2"),
    ))

    assert text == '{"a":1,"b":2}'
    assert receipt["provider_duplicate_output"] is True
    assert receipt["provider_duplicate_semantic_equivalent"] is True


def test_distinct_assistant_messages_fail_closed() -> None:
    with pytest.raises(AzureProviderOutputError) as excinfo:
        canonicalize_structured_output(_response(
            _message('{"action":"read"}', item_id="m1"),
            _message('{"action":"write"}', item_id="m2"),
        ))

    assert excinfo.value.code == "multiple_distinct_assistant_outputs"


def test_mixed_message_and_tool_output_fails_closed() -> None:
    with pytest.raises(AzureProviderOutputError) as excinfo:
        canonicalize_structured_output(_response(
            SimpleNamespace(id="reasoning", type="reasoning", content=[]),
            _message('{"kind":"submit_outcome","summary":"ready"}'),
            SimpleNamespace(id="call-1", type="function_call", name="run_command", arguments="{}"),
        ))

    assert excinfo.value.code == "provider_mixed_message_and_tool_output"


@pytest.mark.parametrize("raw", [
    'leading prose {"a":1}',
    '{"a":1} trailing prose',
    '[{"a":1}]',
    '{"a":1}{"b":2}',
    '```json\n{"a":1}\n```\ntrailing',
])
def test_non_single_object_protocol_is_rejected(raw: str) -> None:
    with pytest.raises(AzureProviderOutputError):
        canonicalize_structured_output(_response(_message(raw)))


def test_one_optional_json_fence_is_accepted() -> None:
    text, _receipt = canonicalize_structured_output(
        _response(_message('```json\n{"summary":"ok","kind":"submit_outcome"}\n```'))
    )
    assert text == '{"kind":"submit_outcome","summary":"ok"}'


class _Responses:
    def __init__(self, job: SimpleNamespace) -> None:
        self.job = job
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.requests.append(dict(kwargs))
        return self.job

    def retrieve(self, job_id: str) -> SimpleNamespace:  # pragma: no cover - terminal fake
        raise AssertionError(job_id)


class _Client:
    def __init__(self, job: SimpleNamespace) -> None:
        self.responses = _Responses(job)


def _model(job: SimpleNamespace) -> AzureModelCallable:
    return AzureModelCallable(
        client=_Client(job),  # type: ignore[arg-type]
        deployment="test",
        effort="low",
        role="solver",
        poll_interval_s=1,
        poll_timeout_s=30,
        max_retries=0,
        sleep=lambda _seconds: None,
    )


def test_incomplete_valid_looking_json_never_returns_as_a_turn() -> None:
    model = _model(_response(
        _message('{"kind":"act","summary":"looks valid","actions":[]}'),
        status="incomplete",
    ))

    with pytest.raises(AzureProviderOutputError) as excinfo:
        model([{"role": "user", "content": "decide"}])

    assert excinfo.value.code == "provider_output_incomplete"
    event = model.drain_telemetry()[0]
    assert event["status"] == "failed"
    assert event["job_status"] == "incomplete"
    assert event["provider_output_error"] == "provider_output_incomplete"
