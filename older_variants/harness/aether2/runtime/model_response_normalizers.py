"""Azure OpenAI response normalizers and message-format helpers.

Extracted from model_routes.py to keep that module under 500 LOC.
These are internal helpers; callers should import from model_routes.py.
Do NOT introduce chatgpt.com, codex OAuth, or app_codex references here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _first_string(value: Any) -> str | None:
    """Return value if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------------------
# Tool normalization helpers
# ---------------------------------------------------------------------------

def _normalize_tool_call(tool_payload: dict[str, Any], *, fallback_type: str) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "type": _first_string(tool_payload.get("type")) or fallback_type,
        "id": _first_string(tool_payload.get("call_id")) or _first_string(tool_payload.get("id")),
        "name": _first_string(tool_payload.get("name")),
        "arguments": tool_payload.get("arguments"),
    }
    return {key: value for key, value in normalized.items() if value is not None}


def _normalize_request_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function_payload = tool.get("function") or {}
            normalized_tool: dict[str, Any] = {"type": "function"}
            name = function_payload.get("name")
            if not isinstance(name, str) or not name:
                continue
            normalized_tool["name"] = name
            description = function_payload.get("description")
            if isinstance(description, str) and description:
                normalized_tool["description"] = description
            parameters = function_payload.get("parameters")
            if isinstance(parameters, dict):
                normalized_tool["parameters"] = parameters
            normalized.append(normalized_tool)
            continue
        if isinstance(tool.get("type"), str):
            normalized.append(dict(tool))
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        normalized_tool: dict[str, Any] = {
            "type": "function",
            "name": name,
        }
        description = tool.get("description")
        if isinstance(description, str) and description:
            normalized_tool["description"] = description
        parameters = tool.get("parameters")
        if isinstance(parameters, dict):
            normalized_tool["parameters"] = parameters
        else:
            input_schema = tool.get("input_schema")
            if isinstance(input_schema, dict):
                normalized_tool["parameters"] = input_schema
        normalized.append(normalized_tool)
    return normalized


def _normalize_chat_completions_tools(tools: Any) -> list[dict[str, Any]]:
    normalized_tools = _normalize_request_tools(tools)
    converted: list[dict[str, Any]] = []
    for tool in normalized_tools:
        if tool.get("type") != "function":
            continue
        name = _first_string(tool.get("name"))
        if not name:
            continue
        function_payload: dict[str, Any] = {"name": name}
        description = _first_string(tool.get("description"))
        if description:
            function_payload["description"] = description
        parameters = tool.get("parameters")
        if isinstance(parameters, dict):
            function_payload["parameters"] = parameters
        converted.append({"type": "function", "function": function_payload})
    return converted


def _normalize_history_tool_calls(tool_calls_payload: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls_payload, list):
        return []
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls_payload:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = _first_string(tool_call.get("id"))
        tool_type = _first_string(tool_call.get("type")) or "function"
        tool_name = _first_string(tool_call.get("name"))
        arguments = tool_call.get("arguments")
        if not tool_name:
            function_payload = tool_call.get("function")
            if isinstance(function_payload, dict):
                tool_name = _first_string(function_payload.get("name"))
                if arguments is None:
                    arguments = function_payload.get("arguments")
        if not tool_name:
            continue
        function_row: dict[str, Any] = {"name": tool_name}
        if isinstance(arguments, str):
            function_row["arguments"] = arguments
        elif arguments is not None:
            function_row["arguments"] = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        row: dict[str, Any] = {"type": tool_type, "function": function_row}
        if tool_call_id:
            row["id"] = tool_call_id
        normalized.append(row)
    return normalized


# ---------------------------------------------------------------------------
# Message normalization helpers
# ---------------------------------------------------------------------------

def _normalize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if not isinstance(role, str) or not role:
            continue
        if role == "assistant":
            tool_calls = _normalize_history_tool_calls(message.get("tool_calls"))
            if tool_calls:
                content = message.get("content")
                if not isinstance(content, str):
                    content = None
                normalized.append({"role": role, "content": content, "tool_calls": tool_calls})
                continue
        if role == "tool":
            content = message.get("content")
            if not isinstance(content, str):
                continue
            tool_call_id = _first_string(message.get("tool_call_id"))
            row = {"role": role, "content": content}
            if tool_call_id:
                row["tool_call_id"] = tool_call_id
            name = _first_string(message.get("name"))
            if name:
                row["name"] = name
            normalized.append(row)
            continue
        content = message.get("content")
        if isinstance(content, str):
            normalized.append({"role": role, "content": content})
            continue
        if isinstance(content, list):
            normalized.append({"role": role, "content": content})
            continue
    return normalized


def _normalize_input_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "assistant":
            content = message.get("content")
            if content is not None:
                normalized.append({"role": role, "content": content})
            tool_calls = _normalize_history_tool_calls(message.get("tool_calls"))
            for tool_call in tool_calls:
                function_payload = tool_call.get("function")
                if not isinstance(function_payload, dict):
                    continue
                name = _first_string(function_payload.get("name"))
                if not name:
                    continue
                row: dict[str, Any] = {
                    "type": "function_call",
                    "name": name,
                    "arguments": function_payload.get("arguments", ""),
                }
                call_id = _first_string(tool_call.get("id"))
                if call_id:
                    row["call_id"] = call_id
                normalized.append(row)
            continue
        if role == "tool":
            tool_call_id = _first_string(message.get("tool_call_id"))
            content = message.get("content")
            if not tool_call_id or content is None:
                continue
            normalized.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": content,
                }
            )
            continue
        content = message.get("content")
        if isinstance(role, str) and content is not None:
            normalized.append({"role": role, "content": content})
    return normalized


def _extract_instructions(
    messages: list[dict[str, Any]],
    route_settings: dict[str, Any],
    kwargs: dict[str, Any],
) -> str:
    explicit = kwargs.get("instructions")
    if isinstance(explicit, str) and explicit.strip():
        return explicit

    route_instruction = route_settings.get("instructions")
    if isinstance(route_instruction, str) and route_instruction.strip():
        return route_instruction

    system_parts: list[str] = []
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            system_parts.append(content.strip())
    if system_parts:
        return "\n\n".join(system_parts)

    return "You are a concise assistant. Follow the user and available tools exactly."


# ---------------------------------------------------------------------------
# Azure response normalizers
# ---------------------------------------------------------------------------

def _extract_text_and_tool_calls(response_payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    output = response_payload.get("output")
    if not isinstance(output, list):
        return "", []

    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"function_call", "tool_call"}:
            tool_calls.append(_normalize_tool_call(item, fallback_type=item_type))

        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            content_type = content_item.get("type")
            if content_type in {"output_text", "text"}:
                text_value = content_item.get("text")
                if isinstance(text_value, str):
                    text_parts.append(text_value)
            if content_type in {"function_call", "tool_call"}:
                tool_calls.append(_normalize_tool_call(content_item, fallback_type=content_type))

    return "".join(text_parts), tool_calls


def _extract_chat_message_text(message_payload: dict[str, Any]) -> str:
    content = message_payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"text", "output_text"}:
            text_value = item.get("text")
            if isinstance(text_value, str):
                text_parts.append(text_value)
    return "".join(text_parts)


def _normalize_azure_tool_calls(tool_calls_payload: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls_payload, list):
        return []
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls_payload:
        if not isinstance(tool_call, dict):
            continue
        function_payload = tool_call.get("function")
        function_payload = function_payload if isinstance(function_payload, dict) else {}
        item = {
            "type": _first_string(tool_call.get("type")) or "function",
            "id": _first_string(tool_call.get("id")),
            "name": _first_string(function_payload.get("name")),
            "arguments": function_payload.get("arguments"),
        }
        normalized.append({key: value for key, value in item.items() if value is not None})
    return normalized


def _normalize_azure_usage(usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = _coerce_int(usage.get("input_tokens"))
    if input_tokens <= 0:
        input_tokens = _coerce_int(usage.get("prompt_tokens"))
    output_tokens = _coerce_int(usage.get("output_tokens"))
    if output_tokens <= 0:
        output_tokens = _coerce_int(usage.get("completion_tokens"))
    total_tokens = _coerce_int(usage.get("total_tokens"))
    cached_input_tokens = _coerce_int(usage.get("cached_input_tokens"))
    if cached_input_tokens <= 0:
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else usage.get("input_tokens_details")
        if isinstance(details, dict):
            cached_input_tokens = _coerce_int(details.get("cached_tokens"))

    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    if cached_input_tokens < 0:
        cached_input_tokens = 0

    return {
        "input_tokens": max(0, input_tokens),
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
    }


def _extract_reasoning_token_count(usage: dict[str, Any]) -> int | None:
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict) and "reasoning_tokens" in completion_details:
        return max(0, _coerce_int(completion_details.get("reasoning_tokens")))
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict) and "reasoning_tokens" in output_details:
        return max(0, _coerce_int(output_details.get("reasoning_tokens")))
    if "reasoning_tokens" in usage:
        return max(0, _coerce_int(usage.get("reasoning_tokens")))
    return None


def _extract_responses_reasoning_telemetry(response_payload: dict[str, Any]) -> dict[str, Any]:
    # Provider-visible reasoning telemetry only: summaries and encrypted continuity metadata.
    # Never persist raw encrypted blobs or imply access to hidden chain-of-thought.
    output = response_payload.get("output")
    if not isinstance(output, list):
        return {}

    summary_parts: list[str] = []
    encrypted_hashes: list[str] = []
    encrypted_chars_total = 0
    reasoning_item_count = 0

    for item in output:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        reasoning_item_count += 1
        summary = item.get("summary")
        if isinstance(summary, list):
            for summary_item in summary:
                if isinstance(summary_item, dict):
                    text = summary_item.get("text")
                    if isinstance(text, str) and text:
                        summary_parts.append(text)
                elif isinstance(summary_item, str) and summary_item:
                    summary_parts.append(summary_item)
        elif isinstance(summary, str) and summary:
            summary_parts.append(summary)

        encrypted_content = item.get("encrypted_content")
        if isinstance(encrypted_content, str) and encrypted_content:
            encrypted_chars_total += len(encrypted_content)
            encrypted_hashes.append(hashlib.sha256(encrypted_content.encode("utf-8")).hexdigest())

    if not summary_parts and not encrypted_hashes and reasoning_item_count == 0:
        return {}

    telemetry: dict[str, Any] = {
        "provider_reasoning": {
            "source": "responses.output.reasoning",
            "reasoning_item_count": reasoning_item_count,
            "summary_count": len(summary_parts),
            "encrypted_item_count": len(encrypted_hashes),
        }
    }
    if summary_parts:
        telemetry["reasoning_summary"] = "\n".join(summary_parts)
    if encrypted_hashes:
        telemetry["reasoning_artifact"] = {
            "type": "encrypted_reasoning_continuity",
            "encoding": "provider_encrypted",
            "encrypted_content_char_count": encrypted_chars_total,
            "encrypted_content_hashes": encrypted_hashes,
        }
    return telemetry


def _normalize_azure_chat_result(*, response: dict[str, Any], model_route: dict[str, Any]) -> dict[str, Any]:
    message_payload: dict[str, Any] = {}
    finish_reason = "stop"
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            finish_reason_value = first_choice.get("finish_reason")
            if isinstance(finish_reason_value, str) and finish_reason_value:
                finish_reason = finish_reason_value
            candidate_message = first_choice.get("message")
            if isinstance(candidate_message, dict):
                message_payload = candidate_message

    text = _extract_chat_message_text(message_payload)
    reasoning_summary = message_payload.get("reasoning_content")
    if not isinstance(reasoning_summary, str) or not reasoning_summary.strip():
        reasoning_summary = None

    tool_calls = _normalize_azure_tool_calls(message_payload.get("tool_calls"))
    usage_raw = response.get("usage")
    usage_dict = usage_raw if isinstance(usage_raw, dict) else {}
    usage = _normalize_azure_usage(usage_dict)
    usage["provider_usage_raw"] = usage_dict
    reasoning_token_count = _extract_reasoning_token_count(usage_dict)

    status = "completed"
    if finish_reason == "length":
        status = "max_tokens_exhausted"
    elif finish_reason == "content_filter":
        status = "blocked"

    normalized = {
        "text": text,
        "tool_calls": tool_calls,
        "usage": usage,
        "status": status,
        "model_route": model_route,
    }
    if reasoning_summary is not None:
        normalized["reasoning_summary"] = reasoning_summary
        normalized["provider_reasoning"] = {
            "source": "message.reasoning_content",
            "summary_count": 1,
        }
    if reasoning_token_count is not None:
        normalized["reasoning_token_count"] = reasoning_token_count
    return normalized


def _normalize_azure_responses_result(*, response: dict[str, Any], model_route: dict[str, Any]) -> dict[str, Any]:
    text, tool_calls = _extract_text_and_tool_calls(response)
    usage_raw = response.get("usage")
    usage_dict = usage_raw if isinstance(usage_raw, dict) else {}
    usage = _normalize_azure_usage(usage_dict)
    usage["provider_usage_raw"] = usage_dict
    status = _first_string(response.get("status")) or "completed"
    reasoning_token_count = _extract_reasoning_token_count(usage_dict)
    reasoning_telemetry = _extract_responses_reasoning_telemetry(response)
    normalized = {
        "text": text,
        "tool_calls": tool_calls,
        "usage": usage,
        "status": status,
        "model_route": model_route,
    }
    if reasoning_token_count is not None:
        normalized["reasoning_token_count"] = reasoning_token_count
    normalized.update(reasoning_telemetry)
    return normalized
