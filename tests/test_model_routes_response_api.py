from __future__ import annotations

import sys
import types

from harness.aether2.runtime.model_routes import (
    AZURE_OPENAI_RESPONSES_API_VERSION,
    AzureOpenAIAPIKeyModelClient,
    make_azure_openai_route,
    make_azure_gpt54_pro_route_from_env,
)
from harness.aether2.runtime.model_client import _tools_for_route


def test_gpt53_codex_route_uses_responses_api_version_floor() -> None:
    route = make_azure_openai_route(
        endpoint="https://example.openai.azure.com",
        deployment="gpt-5.3-codex",
        api_key_env_var="AZURE_OPENAI_GPT53_CODEX_KEY",
        pricing_model_id="gpt-5.3-codex",
        api_version="2024-12-01-preview",
    )

    settings = route["request_settings"]
    assert settings["azure_api_surface"] == "v1_responses"
    assert settings["azure_api_version"] == AZURE_OPENAI_RESPONSES_API_VERSION


def test_gpt54_mini_route_keeps_chat_api_version() -> None:
    route = make_azure_openai_route(
        endpoint="https://example.openai.azure.com",
        deployment="gpt-5.4-mini",
        api_key_env_var="AZURE_OPENAI_GPT54_MINI_KEY",
        pricing_model_id="gpt-5.4-mini",
        api_version="2024-12-01-preview",
    )

    settings = route["request_settings"]
    assert settings["azure_api_surface"] == "deployment_chat_completions"
    assert settings["azure_api_version"] == "2024-12-01-preview"


def test_gpt54_pro_route_uses_responses_surface_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_DEPLOYMENT", "gpt-5.4-pro")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_API_SURFACE", "v1_responses")

    route = make_azure_gpt54_pro_route_from_env(request_settings={"temperature": 0})

    settings = route["request_settings"]
    assert route["model_name"] == "gpt-5.4-pro"
    assert settings["azure_api_surface"] == "v1_responses"
    assert settings["azure_api_version"] == AZURE_OPENAI_RESPONSES_API_VERSION
    assert route["api_base"] == "https://example.openai.azure.com/openai/v1/responses"
    assert "temperature" not in settings


def test_gpt54_pro_route_defaults_to_responses_surface(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_DEPLOYMENT", "gpt-5.4-pro")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_KEY", "test-key")
    monkeypatch.delenv("AZURE_OPENAI_GPT54_PRO_API_SURFACE", raising=False)

    route = make_azure_gpt54_pro_route_from_env()

    settings = route["request_settings"]
    assert settings["azure_api_surface"] == "v1_responses"
    assert settings["azure_api_version"] == AZURE_OPENAI_RESPONSES_API_VERSION
    assert route["api_base"] == "https://example.openai.azure.com/openai/v1/responses"
    assert settings["reasoning"]["effort"] == "xhigh"


def test_gpt54_pro_route_preserves_explicit_reasoning_override(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_DEPLOYMENT", "gpt-5.4-pro")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_KEY", "test-key")

    route = make_azure_gpt54_pro_route_from_env(
        request_settings={"reasoning": {"effort": "medium"}}
    )

    settings = route["request_settings"]
    assert settings["reasoning"]["effort"] == "medium"


def test_gpt54_pro_route_upgrades_older_api_version_for_responses_surface(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_DEPLOYMENT", "gpt-5.4-pro")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_API_SURFACE", "v1_responses")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    route = make_azure_gpt54_pro_route_from_env()

    settings = route["request_settings"]
    assert settings["azure_api_surface"] == "v1_responses"
    assert settings["azure_api_version"] == AZURE_OPENAI_RESPONSES_API_VERSION


def test_gpt54_pro_route_normalizes_full_endpoint_url(monkeypatch) -> None:
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/responses?api-version=2025-04-01-preview",
    )
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_DEPLOYMENT", "gpt-5.4-pro")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_KEY", "test-key")

    route = make_azure_gpt54_pro_route_from_env()

    settings = route["request_settings"]
    assert settings["azure_endpoint"] == "https://example.openai.azure.com"
    assert settings["azure_api_surface"] == "v1_responses"
    assert route["api_base"] == "https://example.openai.azure.com/openai/v1/responses"


def test_gpt53_client_normalizes_chat_shaped_payload_from_responses_surface(monkeypatch) -> None:  # noqa: ANN001
    route = make_azure_openai_route(
        endpoint="https://example.openai.azure.com",
        deployment="gpt-5.3-codex",
        api_key_env_var="AZURE_OPENAI_GPT53_CODEX_KEY",
        pricing_model_id="gpt-5.3-codex",
        api_version="2025-03-01-preview",
    )
    client = AzureOpenAIAPIKeyModelClient(route=route)

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "PREFLIGHT_OK"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                "model": "gpt-5.3-codex",
            }

    monkeypatch.setenv("AZURE_OPENAI_GPT53_CODEX_KEY", "test-key")
    class _FakeResponses:
        def create(self, **kwargs):  # noqa: ANN001
            return _Response()

    class _FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN001
            self.responses = _FakeResponses()

    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    result = client.complete([{"role": "user", "content": "hi"}])
    assert result["text"] == "PREFLIGHT_OK"


def test_aether_client_preserves_nested_tools_for_litellm_responses_bridge() -> None:
    route = make_azure_openai_route(
        endpoint="https://example.openai.azure.com",
        deployment="gpt-5.3-codex",
        api_key_env_var="AZURE_OPENAI_GPT53_CODEX_KEY",
        pricing_model_id="gpt-5.3-codex",
        api_version="2025-03-01-preview",
    )
    nested_tool = {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command.",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    tools = _tools_for_route(route, [nested_tool])

    assert tools == [nested_tool]


def test_aether_client_flattens_nested_tools_for_chat_completions_route() -> None:
    route = make_azure_openai_route(
        endpoint="https://example.openai.azure.com",
        deployment="gpt-5.4-mini",
        api_key_env_var="AZURE_OPENAI_GPT54_MINI_KEY",
        pricing_model_id="gpt-5.4-mini",
        api_version="2024-12-01-preview",
    )
    nested_tool = {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command.",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    tools = _tools_for_route(route, [nested_tool])

    assert tools == [
        {
            "type": "function",
            "name": "run_command",
            "description": "Run a command.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_aether_client_preserves_nested_tools_for_pro_responses_route(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_DEPLOYMENT", "gpt-5.4-pro")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_GPT54_PRO_API_SURFACE", "v1_responses")

    route = make_azure_gpt54_pro_route_from_env()
    nested_tool = {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command.",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    tools = _tools_for_route(route, [nested_tool])

    assert tools == [nested_tool]
