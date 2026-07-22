from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_verifier_knownbad_eval.py"
_SPEC = importlib.util.spec_from_file_location("run_verifier_knownbad_eval", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_workspace_translation_failure_invalidates_model_measurement() -> None:
    valid, issues = _MODULE._inspection_environment_validity([{
        "requests": [],
        "results": [{
            "kind": "overlay_run_command",
            "stderr": "bash: line 1: cd: /app: No such file or directory",
        }],
    }])

    assert valid is False
    assert issues == ("inspection_workspace_path_unavailable",)


def test_task_inspection_failure_without_workspace_translation_stays_scoreable() -> None:
    valid, issues = _MODULE._inspection_environment_validity([{
        "requests": [],
        "results": [{
            "kind": "probe_port",
            "stdout": "closed rc=111",
        }],
    }])

    assert valid is True
    assert issues == ()


def test_missing_vision_route_invalidates_the_evaluator_measurement() -> None:
    valid, issues = _MODULE._inspection_environment_validity([{
        "requests": [],
        "results": [{
            "kind": "perceive_artifact",
            "error": "no vision model available for perceive_artifact",
        }],
    }])

    assert valid is False
    assert issues == ("inspection_vision_route_unavailable",)


def test_bounded_verifier_inspection_exhaustion_is_scoreable_protocol_failure() -> None:
    from aether_next.model_hooks import ModelOutputError

    assert _MODULE._is_bounded_verifier_protocol_failure(ModelOutputError(
        "verifier exceeded bounded inspection rounds without returning a verdict"
    )) is True
    assert _MODULE._is_bounded_verifier_protocol_failure(ModelOutputError(
        "verifier output could not be parsed: invalid JSON"
    )) is False


def test_historical_launch_extraction_replays_only_explicit_background_command() -> None:
    commands = _MODULE._historical_launch_commands({
        "steps": [{
            "turn": {
                "actions": [{
                    "arguments": {
                        "command": (
                            "python3 prepare.py\n"
                            "nohup python3 /app/server.py >/app/server.log 2>&1 &\n"
                            "python3 probe.py"
                        ),
                    },
                }],
            },
        }],
    })

    assert commands == ("nohup python3 /app/server.py >/app/server.log 2>&1 &",)


def test_historical_launch_extraction_uses_full_execution_receipt_when_action_is_truncated() -> None:
    commands = _MODULE._historical_launch_commands({
        "steps": [{
            "turn": {"actions": [{"arguments": {"command": "build... [truncated]"}}]},
            "observations": [{
                "summary": (
                    "command exit=0: python3 build.py\n"
                    "nohup python3 /app/server.py >/app/server.log 2>&1 &"
                ),
            }],
        }],
    })

    assert commands == ("nohup python3 /app/server.py >/app/server.log 2>&1 &",)


def test_no_historical_launch_is_recorded_not_treated_as_a_replay_failure() -> None:
    receipts = _MODULE._restore_historical_launches(None, ())

    assert receipts == [{
        "kind": "historical_process_restore",
        "status": "not_applicable",
        "reason": "no_explicit_background_launch_in_trace",
    }]


def test_model_telemetry_fields_are_hash_only_and_route_diagnostic() -> None:
    receipt = {
        "event_kind": "provider_attempt",
        "job_id": "opaque-job-id",
        "candidate_hashes": ["abc123"],
        "candidate_message_count": 1,
        "text": "must-not-be-recorded",
    }

    captured = _MODULE._hash_only_provider_telemetry([receipt])

    assert captured == [{
        "event_kind": "provider_attempt",
        "job_id": "opaque-job-id",
        "candidate_hashes": ["abc123"],
        "candidate_message_count": 1,
    }]


def test_verifier_uses_dedicated_semantic_vision_callable(monkeypatch) -> None:
    from aether_next.providers import azure_model

    calls: list[tuple[str, dict]] = []
    text_route = object()
    vision_route = object()

    def text_factory(**kwargs):
        calls.append(("text", kwargs))
        return text_route

    def vision_factory(**kwargs):
        calls.append(("vision", kwargs))
        return vision_route

    monkeypatch.setattr(azure_model, "make_azure_callable", text_factory)
    monkeypatch.setattr(azure_model, "make_azure_vision_callable", vision_factory)
    verifier, vision = _MODULE._model_callables(SimpleNamespace(
        deploy_env="TEXT_DEPLOYMENT", key_env="KEY", endpoint_env="ENDPOINT",
        vision_deploy_env="VISION_DEPLOYMENT",
    ))

    assert verifier is text_route
    assert vision is vision_route
    assert calls == [
        ("text", {"deployment_env": "TEXT_DEPLOYMENT", "key_env": "KEY", "endpoint_env": "ENDPOINT", "role": "verifier"}),
        ("vision", {"deployment_env": "VISION_DEPLOYMENT", "key_env": "KEY", "endpoint_env": "ENDPOINT"}),
    ]
