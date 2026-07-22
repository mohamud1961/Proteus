"""Unit tests for _split_messages — no network, pure logic only."""
from __future__ import annotations

from aether_next.providers.azure_model import _split_messages, _split_responses_input


class TestSplitMessagesMixed:
    """Mixed system + non-system messages (happy path)."""

    def test_system_goes_to_instructions_user_goes_to_input(self) -> None:
        messages = [
            {"role": "system", "content": "A"},
            {"role": "user", "content": "B"},
        ]
        instructions, input_text = _split_messages(messages)
        assert "A" in instructions
        assert "B" in input_text


class TestSplitMessagesAllSystem:
    """All-system messages — the solver bug that produced empty input."""

    def test_input_is_non_empty(self) -> None:
        messages = [
            {"role": "system", "content": "P1"},
            {"role": "system", "content": "P2"},
            {"role": "system", "content": "SOLVER"},
        ]
        instructions, input_text = _split_messages(messages)
        assert input_text, "input must be non-empty (the original bug)"
        assert instructions, "instructions must be non-empty"

    def test_last_system_becomes_input(self) -> None:
        messages = [
            {"role": "system", "content": "P1"},
            {"role": "system", "content": "P2"},
            {"role": "system", "content": "SOLVER"},
        ]
        instructions, input_text = _split_messages(messages)
        assert input_text == "SOLVER"
        assert "P1" in instructions
        assert "P2" in instructions
        # The solver prompt must NOT also appear in instructions.
        assert "SOLVER" not in instructions


class TestSplitMessagesSingleSystem:
    """Exactly one system message, nothing else."""

    def test_input_is_non_empty(self) -> None:
        messages = [{"role": "system", "content": "Only one"}]
        instructions, input_text = _split_messages(messages)
        assert input_text, "input must be non-empty"
        assert input_text == "Only one"


class TestSplitMessagesEmpty:
    """No messages at all — must still produce non-empty input."""

    def test_input_is_non_empty(self) -> None:
        instructions, input_text = _split_messages([])
        assert input_text, "input must be non-empty even with no messages"


def test_responses_input_preserves_assistant_and_user_roles() -> None:
    instructions, input_items = _split_responses_input([
        {"role": "system", "content": "Verifier contract"},
        {"role": "user", "content": "Inspect the workspace"},
        {"role": "assistant", "content": '{"kind":"inspect"}'},
        {"role": "user", "content": "Here are the observations"},
    ])

    assert instructions == "Verifier contract"
    assert input_items == [
        {"role": "user", "content": "Inspect the workspace"},
        {"role": "assistant", "content": '{"kind":"inspect"}'},
        {"role": "user", "content": "Here are the observations"},
    ]
