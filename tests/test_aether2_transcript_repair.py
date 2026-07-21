"""Regression tests for the tool-call/response pairing invariant (Class-B fix).

Context: a Harbor board surfaced a real harness bug — assistant `tool_calls`
reaching the provider without matching `tool` responses raised an Azure 400 and
aborted the run. These tests pin the invariant down and prove the known-bad
transcript can no longer reach a provider.
"""

from __future__ import annotations

from typing import Any

from harness.aether2.runtime.model_client import Aether2ModelClient
from harness.aether2.runtime.transcript_repair import (
    DROPPED_ORPHAN,
    REPAIR_NOTICE,
    SYNTHESIZED,
    repair_tool_call_pairs,
    serialize_parallel_tool_calls,
)


def _assistant(call_ids: list[str], *, text: str = "") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": f"tool_{cid}", "arguments": "{}"}}
            for cid in call_ids
        ],
    }


def _tool(call_id: str, *, name: str = "", content: str = "ok") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "tool", "tool_call_id": call_id, "content": content}
    if name:
        msg["name"] = name
    return msg


def _ids_with_responses(messages: list[dict[str, Any]]) -> bool:
    """True iff every assistant tool_call_id has a following tool response."""
    answered: set[str] = set()
    for message in messages:
        if message.get("role") == "tool":
            answered.add(str(message.get("tool_call_id")))
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                if str(call.get("id")) not in answered:
                    return False
    return True


# 1. Normal paired assistant/tool messages are unchanged (identity / no-op).
def test_valid_transcript_is_unchanged():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        _assistant(["a"]),
        _tool("a", name="tool_a"),
        {"role": "assistant", "content": "done"},
    ]
    result = repair_tool_call_pairs(messages)
    assert not result.repaired
    assert result.events == ()
    assert result.messages == messages


def test_valid_parallel_tool_calls_unchanged():
    messages = [
        _assistant(["a", "b"]),
        _tool("a"),
        _tool("b"),
        {"role": "assistant", "content": "ok"},
    ]
    result = repair_tool_call_pairs(messages)
    assert not result.repaired
    assert _ids_with_responses(result.messages)


# 2 & 3. Missing tool response is repaired with a synthetic, evidence-safe message.
def test_missing_tool_response_is_synthesized_and_marked_not_evidence():
    # This is the exact bug shape: assistant tool_calls followed immediately by
    # the next assistant turn, with no tool response in between.
    messages = [
        _assistant(["call_kXW"], text="running"),
        {"role": "assistant", "content": "next turn"},
    ]
    result = repair_tool_call_pairs(messages)
    assert result.repaired
    assert _ids_with_responses(result.messages)
    synth = [m for m in result.messages if m.get("role") == "tool"]
    assert len(synth) == 1
    assert synth[0]["tool_call_id"] == "call_kXW"
    assert REPAIR_NOTICE == synth[0]["content"]
    assert "do not treat" in synth[0]["content"].lower()
    kinds = {event.kind for event in result.events}
    assert kinds == {SYNTHESIZED}


def test_partial_parallel_responses_fills_only_the_gap():
    messages = [
        _assistant(["a", "b", "c"]),
        _tool("a"),
        _tool("c"),
        {"role": "assistant", "content": "next"},
    ]
    result = repair_tool_call_pairs(messages)
    assert _ids_with_responses(result.messages)
    synthesized = [e for e in result.events if e.kind == SYNTHESIZED]
    assert [e.tool_call_id for e in synthesized] == ["b"]
    # Real responses for a and c are preserved (not replaced by synthetics).
    real = [m for m in result.messages if m.get("role") == "tool" and m["content"] != REPAIR_NOTICE]
    assert {m["tool_call_id"] for m in real} == {"a", "c"}


# 4. Orphan tool response is removed from the model-visible transcript.
def test_orphan_tool_response_is_dropped():
    messages = [
        {"role": "user", "content": "hi"},
        _tool("ghost", content="result with no owning assistant call"),
        {"role": "assistant", "content": "ok"},
    ]
    result = repair_tool_call_pairs(messages)
    assert result.repaired
    assert all(m.get("role") != "tool" for m in result.messages)
    assert [e.kind for e in result.events] == [DROPPED_ORPHAN]
    assert result.events[0].tool_call_id == "ghost"


# 5. A truncation/compaction split (assistant kept, responses dropped) is repaired,
#    never sent with one side of the pair missing.
def test_truncation_split_is_repaired():
    # Simulate a tail truncation that dropped the tool responses but kept the
    # assistant tool_calls message and later turns.
    messages = [
        _assistant(["x", "y"]),
        {"role": "assistant", "content": "later work"},
        _assistant(["z"]),
        {"role": "user", "content": "more"},
    ]
    result = repair_tool_call_pairs(messages)
    assert _ids_with_responses(result.messages)
    assert {e.tool_call_id for e in result.events if e.kind == SYNTHESIZED} == {"x", "y", "z"}


# 6. A rebase/verification-style branch that left an unanswered tool_call cannot
#    produce an invalid transcript once it passes through the invariant.
def test_branch_left_unanswered_tool_call_is_made_valid():
    # Assistant emits tool_calls, branch breaks to a system note + next turn
    # without executing — the historical orphan shape.
    messages = [
        _assistant(["pending"], text="requesting verification"),
        {"role": "system", "content": "[verifier] please verify"},
        {"role": "assistant", "content": "verifying"},
    ]
    result = repair_tool_call_pairs(messages)
    assert _ids_with_responses(result.messages)
    # The intervening system note is preserved, not dropped.
    assert any(m.get("role") == "system" for m in result.messages)


def test_idempotent():
    messages = [_assistant(["a"]), {"role": "assistant", "content": "next"}]
    once = repair_tool_call_pairs(messages)
    twice = repair_tool_call_pairs(once.messages)
    assert not twice.repaired
    assert twice.messages == once.messages


# 7. The known-bad transcript no longer reaches the provider with the protocol
#    violation. We assert the model client sends a repaired, valid transcript.
class _CapturingProvider:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] | None = None

    def complete(self, messages: list[dict[str, Any]], *, tools: Any) -> dict[str, Any]:
        self.sent_messages = messages
        return {"text": "ok", "tool_calls": [], "usage": {}, "status": "ok"}


def _model_client_with(provider: _CapturingProvider) -> Aether2ModelClient:
    client = Aether2ModelClient.__new__(Aether2ModelClient)
    client.model_route = {}
    client.max_attempts = 1
    client.backoff_sec = 0.0
    client._client = provider
    client.transcript_repair_events = []
    return client


def test_model_client_repairs_known_bad_before_send():
    provider = _CapturingProvider()
    client = _model_client_with(provider)
    bad = [
        {"role": "user", "content": "go"},
        _assistant(["call_kXW4zoWpbSuv3nmecIvxK3Tj"], text="acting"),
        {"role": "assistant", "content": "implicitly continuing"},
    ]
    client.call(bad, [], cache_prefix_len=0)
    assert provider.sent_messages is not None
    assert _ids_with_responses(provider.sent_messages)
    assert client.transcript_repairs == 1


def test_model_client_noop_for_valid_transcript():
    provider = _CapturingProvider()
    client = _model_client_with(provider)
    good = [_assistant(["a"]), _tool("a"), {"role": "assistant", "content": "done"}]
    client.call(good, [], cache_prefix_len=0)
    assert provider.sent_messages == good
    assert client.transcript_repairs == 0


def test_parallel_tool_calls_are_serialized_for_provider_history():
    messages = [
        {"role": "user", "content": "go"},
        _assistant(["a", "b", "c"], text="parallel work"),
        _tool("a", content="result a"),
        _tool("b", content="result b"),
        _tool("c", content="result c"),
        {"role": "system", "content": "tail"},
    ]

    serialized = serialize_parallel_tool_calls(messages)

    assert [m.get("role") for m in serialized] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "system",
    ]
    assistant_calls = [
        m["tool_calls"][0]["id"]
        for m in serialized
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    tool_responses = [m.get("tool_call_id") for m in serialized if m.get("role") == "tool"]
    assert assistant_calls == ["a", "b", "c"]
    assert tool_responses == ["a", "b", "c"]
    assert _ids_with_responses(serialized)


def test_model_client_serializes_parallel_tool_history_before_send():
    provider = _CapturingProvider()
    client = _model_client_with(provider)
    messages = [
        {"role": "user", "content": "go"},
        _assistant(
            [
                "call_IdX20IY98ahEhcaWpKAmAYR5",
                "call_9qEbstHyYBTovDpecysZiwZ1",
                "call_5twfaAgbyG7lKo97GcRLwGrG",
            ],
            text="parallel work",
        ),
        _tool("call_IdX20IY98ahEhcaWpKAmAYR5", content="result 1"),
        _tool("call_9qEbstHyYBTovDpecysZiwZ1", content="result 2"),
        _tool("call_5twfaAgbyG7lKo97GcRLwGrG", content="result 3"),
    ]

    client.call(messages, [], cache_prefix_len=0)

    assert provider.sent_messages is not None
    assistant_calls = [
        m["tool_calls"][0]["id"]
        for m in provider.sent_messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_calls == [
        "call_IdX20IY98ahEhcaWpKAmAYR5",
        "call_9qEbstHyYBTovDpecysZiwZ1",
        "call_5twfaAgbyG7lKo97GcRLwGrG",
    ]
    assert _ids_with_responses(provider.sent_messages)
    assert client.transcript_repairs == 0
