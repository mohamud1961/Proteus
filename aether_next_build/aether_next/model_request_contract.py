"""Certified model-request realisation preflight.

A compiled runtime setting is not real until the provider callable proves the
same value will reach the outgoing request. Certified runs reject opaque or
clamping wrappers before model credits are spent.
"""
from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ExpectedModelRequest:
    logical_role: str
    max_output_tokens: int


class ModelRequestRealizationError(ValueError):
    """Raised when a callable cannot prove its outgoing request contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _native_azure_preflight(
    model: Any,
    expected: ExpectedModelRequest,
) -> Mapping[str, Any] | None:
    """Certify the repository's native Azure callable, never arbitrary wrappers.

    The native class is audited to forward ``max_output_tokens`` directly into
    the Responses request. A wrapper or subclass is not accepted through this
    path because it may clamp or rewrite the value.
    """
    cls = model.__class__
    if cls.__module__ != "aether_next.providers.azure_model" or cls.__name__ != "AzureModelCallable":
        return None
    try:
        source = inspect.getsource(cls._call_once)
    except (OSError, TypeError, AttributeError) as exc:
        raise ModelRequestRealizationError(
            "model_request_preflight_unavailable",
            f"cannot inspect native Azure request builder: {exc}",
        ) from exc
    required_fragments = (
        '"max_output_tokens": max_output_tokens',
        '"text": {"format": {"type": "json_object"}}',
        "instructions = _prepare_json_object_instructions(instructions)",
        "user_input = _prepare_json_object_input(user_input)",
    )
    if any(fragment not in source for fragment in required_fragments):
        raise ModelRequestRealizationError(
            "model_request_preflight_invalid",
            "native Azure request builder no longer satisfies the structured-output contract",
        )
    return {
        "provider": "azure_openai_responses",
        "model": str(getattr(model, "_deployment", "")).strip(),
        "provider_role": str(getattr(model, "_role", "")).strip(),
        "effort": str(getattr(model, "_effort", "")).strip(),
        "max_output_tokens": int(expected.max_output_tokens),
        "background": True,
        "structured_output_mode": "json_object",
        "explicit_json_instruction": True,
        "json_instruction_in_input": True,
        "certification": "native_request_builder_source_contract",
    }


def preflight_model_request(
    model: Any,
    expected: ExpectedModelRequest,
) -> dict[str, Any]:
    """Validate one logical role against a certified request builder."""
    describe = getattr(model, "preflight_request", None)
    if callable(describe):
        realized = describe(
            max_output_tokens=int(expected.max_output_tokens),
            logical_role=expected.logical_role,
        )
    else:
        realized = _native_azure_preflight(model, expected)
    if not isinstance(realized, Mapping):
        raise ModelRequestRealizationError(
            "model_request_preflight_unavailable",
            f"{expected.logical_role} callable exposes no certified request contract",
        )
    actual_tokens = realized.get("max_output_tokens")
    try:
        actual_tokens_int = int(actual_tokens)
    except (TypeError, ValueError):
        raise ModelRequestRealizationError(
            "model_request_preflight_invalid",
            f"{expected.logical_role} preflight omitted an integer max_output_tokens",
        ) from None
    if actual_tokens_int != int(expected.max_output_tokens):
        raise ModelRequestRealizationError(
            "model_request_output_budget_mismatch",
            (
                f"{expected.logical_role} compiled max_output_tokens="
                f"{expected.max_output_tokens} but outgoing request would use {actual_tokens_int}"
            ),
        )
    provider = str(realized.get("provider", "")).strip()
    model_name = str(realized.get("model", "")).strip()
    if not provider or not model_name:
        raise ModelRequestRealizationError(
            "model_request_preflight_invalid",
            f"{expected.logical_role} preflight must identify provider and model",
        )
    if provider == "azure_openai_responses":
        if (
            realized.get("structured_output_mode") != "json_object"
            or not bool(realized.get("explicit_json_instruction"))
            or not bool(realized.get("json_instruction_in_input"))
        ):
            raise ModelRequestRealizationError(
                "model_request_json_contract_invalid",
                f"{expected.logical_role} Azure preflight must prove json_object mode and explicit JSON instruction in input",
            )
    return {
        "logical_role": expected.logical_role,
        "expected_max_output_tokens": int(expected.max_output_tokens),
        "actual_max_output_tokens": actual_tokens_int,
        "provider": provider,
        "model": model_name,
        "provider_role": str(realized.get("provider_role", "")).strip(),
        "effort": str(realized.get("effort", "")).strip(),
        "background": bool(realized.get("background", False)),
        "structured_output_mode": str(realized.get("structured_output_mode", "")).strip(),
        "explicit_json_instruction": bool(realized.get("explicit_json_instruction", False)),
        "json_instruction_in_input": bool(realized.get("json_instruction_in_input", False)),
        "certification": str(realized.get("certification", "explicit_preflight_contract")),
        "status": "matched",
    }


def preflight_model_requests(
    requests: Iterable[tuple[Any, ExpectedModelRequest]],
) -> tuple[dict[str, Any], ...]:
    """Validate all logical roles, preserving exact realised rows."""
    return tuple(preflight_model_request(model, expected) for model, expected in requests)
