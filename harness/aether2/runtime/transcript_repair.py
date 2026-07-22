"""Tool-call/response pairing invariant for model transcripts.

OpenAI / Azure require that every assistant message carrying ``tool_calls`` is
followed by exactly one ``tool`` message per ``tool_call_id`` before the next
assistant turn, and that every ``tool`` message responds to a real prior
``tool_call_id``. Aether-2 assembles transcripts across several branches
(normal tool execution, verification break, rebase, compaction, dynamic-tail
truncation). Any branch that keeps an assistant ``tool_calls`` message but
drops or skips its matching ``tool`` response — or that strands a ``tool``
response after truncation — produces a protocol-invalid transcript. Sending it
raises a provider ``400`` and aborts the run.

This module is the single pre-send invariant. Given an assembled message list
it returns a protocol-valid list plus an audit record of every repair. It is a
strict no-op for already-valid transcripts (the same list is returned), so the
flag-off baseline prefix/digest is unaffected and valid runs are byte-identical.

Repair policy (truthful — never fabricates task evidence):

- Assistant ``tool_call_id`` with no ``tool`` response -> synthesize a ``tool``
  message marked ``transcript_repair`` / not executed. This satisfies the
  protocol without claiming the tool ran or inventing an outcome.
- ``tool`` message with no matching prior assistant ``tool_call`` (or a
  duplicate response) -> drop from the model-visible transcript. The caller
  retains the underlying observation in the evidence ledger / raw logs.
- Real tool responses are grouped immediately after their owning assistant
  message, so a truncation/compaction split can never present one side of a
  pair without the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

REPAIR_NOTICE = (
    "[transcript_repair] No tool response was recorded for this tool_call before the "
    "transcript was assembled for the model. The tool was not executed, or its result "
    "was lost during harness transcript assembly. This message exists only to satisfy "
    "the tool-call protocol; do not treat it as task evidence."
)

SYNTHESIZED = "synthesized_missing_tool_response"
DROPPED_ORPHAN = "dropped_orphan_tool_response"


@dataclass(frozen=True)
class RepairEvent:
    kind: str
    tool_call_id: str
    tool_name: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "tool_call_id": self.tool_call_id, "tool_name": self.tool_name}


@dataclass(frozen=True)
class RepairResult:
    messages: list[dict[str, Any]]
    events: tuple[RepairEvent, ...]

    @property
    def repaired(self) -> bool:
        return bool(self.events)


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _assistant_tool_call_ids(message: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Ordered (tool_call_id, tool_name) pairs for an assistant message."""
    calls = message.get("tool_calls")
    out: list[tuple[str, str]] = []
    if not isinstance(calls, list):
        return out
    for call in calls:
        if not _is_mapping(call):
            continue
        cid = call.get("id")
        if cid is None:
            continue
        name = ""
        fn = call.get("function")
        if _is_mapping(fn):
            name = str(fn.get("name", "") or "")
        if not name:
            name = str(call.get("name", "") or "")
        out.append((str(cid), name))
    return out


def _is_assistant_with_tool_calls(message: Any) -> bool:
    return _is_mapping(message) and message.get("role") == "assistant" and bool(message.get("tool_calls"))


def _synthetic_tool_response(tool_call_id: str, tool_name: str) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": REPAIR_NOTICE,
    }
    if tool_name:
        message["name"] = tool_name
    return message


def repair_tool_call_pairs(messages: Sequence[Mapping[str, Any]]) -> RepairResult:
    """Return a protocol-valid copy of ``messages`` plus the repairs applied.

    Idempotent: a transcript that is already valid is returned unchanged (same
    list object content and order), with no events.
    """
    src: list[dict[str, Any]] = [dict(m) if _is_mapping(m) else m for m in messages]
    events: list[RepairEvent] = []
    out: list[dict[str, Any]] = []
    n = len(src)
    i = 0
    while i < n:
        message = src[i]
        if _is_assistant_with_tool_calls(message):
            ids = _assistant_tool_call_ids(message)
            id_set = {cid for cid, _ in ids}
            # Window = everything up to (but not including) the next assistant message.
            j = i + 1
            window: list[Any] = []
            while j < n:
                nxt = src[j]
                if _is_mapping(nxt) and nxt.get("role") == "assistant":
                    break
                window.append(nxt)
                j += 1
            responses_by_id: dict[str, Any] = {}
            passthrough: list[Any] = []
            for item in window:
                if _is_mapping(item) and item.get("role") == "tool":
                    raw_id = item.get("tool_call_id")
                    wid = None if raw_id is None else str(raw_id)
                    if wid in id_set and wid not in responses_by_id:
                        responses_by_id[wid] = item
                    else:
                        events.append(RepairEvent(DROPPED_ORPHAN, "" if wid is None else wid))
                else:
                    passthrough.append(item)
            out.append(message)
            for cid, name in ids:
                if cid in responses_by_id:
                    out.append(responses_by_id[cid])
                else:
                    out.append(_synthetic_tool_response(cid, name))
                    events.append(RepairEvent(SYNTHESIZED, cid, name))
            out.extend(passthrough)
            i = j
        elif _is_mapping(message) and message.get("role") == "tool":
            # A tool response with no owning assistant tool_calls in scope is an orphan.
            raw_id = message.get("tool_call_id")
            events.append(RepairEvent(DROPPED_ORPHAN, "" if raw_id is None else str(raw_id)))
            i += 1
        else:
            out.append(message)
            i += 1
    if not events:
        return RepairResult(messages=src, events=())
    return RepairResult(messages=out, events=tuple(events))


def serialize_parallel_tool_calls(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return provider-conservative history with one tool call per assistant turn.

    Some Azure/LiteLLM chat-completions paths have rejected an otherwise valid
    assistant turn containing three parallel ``tool_calls`` as if only the first
    tool response counted. Receipts should preserve the model's original
    parallel intent, but the provider-visible transcript can be represented more
    conservatively as repeated assistant/tool pairs. This does not invent task
    evidence; each split assistant keeps one original tool call and its matching
    real or repair-generated tool response.
    """
    repaired = repair_tool_call_pairs(messages).messages
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(repaired):
        message = repaired[i]
        if not _is_assistant_with_tool_calls(message):
            out.append(dict(message))
            i += 1
            continue

        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) <= 1:
            out.append(dict(message))
            i += 1
            continue

        ids = _assistant_tool_call_ids(message)
        expected_ids = [cid for cid, _ in ids]
        responses: list[dict[str, Any]] = []
        j = i + 1
        while j < len(repaired):
            candidate = repaired[j]
            if not (_is_mapping(candidate) and candidate.get("role") == "tool"):
                break
            if str(candidate.get("tool_call_id", "")) not in expected_ids:
                break
            responses.append(dict(candidate))
            j += 1
            if len(responses) >= len(expected_ids):
                break

        if len(responses) != len(expected_ids):
            # Should not happen after repair_tool_call_pairs(), but keep the
            # original shape if an unexpected future format appears.
            out.append(dict(message))
            i += 1
            continue

        responses_by_id = {str(response.get("tool_call_id")): response for response in responses}
        for call in calls:
            if not _is_mapping(call):
                continue
            call_id = call.get("id")
            if call_id is None:
                continue
            cid = str(call_id)
            split_message = dict(message)
            split_message["tool_calls"] = [dict(call)]
            out.append(split_message)
            out.append(responses_by_id[cid])
        i = j
    return out
