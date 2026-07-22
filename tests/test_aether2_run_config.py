from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.aether2.control.ahp_preflight import run_preflight
from harness.aether2.control.action_helpers import _build_blind_retry_blocked_envelope
from harness.aether2.control.completion import _build_completion_contract
from harness.aether2.runtime.adaptive_artifacts import build_config_realization_audit, write_ahp_artifacts
from harness.aether2.runtime.adaptive_context import apply_adaptation_contract, generate_and_apply
from harness.aether2.runtime.adaptive_profile import (
    AgentInitializationFailure,
    ProfileGenerationResult,
    ProfileValidationResult,
    validate_profile,
)
from harness.aether2.runtime.context import ContextManager
from harness.aether2.runtime.model_client import ModelResponse
from harness.aether2.runtime.prompts import MECHANICAL_SYSTEM_PROMPT, SYSTEM_PROMPT
from harness.aether2.runtime.run_config import (
    INVARIANT_CONTEXT_PACK_SECTIONS,
    build_baseline_run_config,
    make_harness_run_config,
    tool_names_from_schemas,
    validate_context_pack_policy,
)
from harness.aether2.runtime.verify import CheckResult, verify_fresh_context


_SAMPLE_TASK = "Create hello.py and make it print Hello, World!."
_SAMPLE_STATED_REQUIREMENTS = ["Create hello.py", "hello.py prints Hello, World!"]
_SAMPLE_ORIENTATION = {
    "cwd": "/workspace",
    "workspace_root": "/workspace",
    "runtimes": {"python3": "Python 3.11.0"},
}
_SAMPLE_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "run_command", "description": "Run shell", "parameters": {}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read file", "parameters": {}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write file", "parameters": {}}},
    {"type": "function", "function": {"name": "task_done", "description": "Done", "parameters": {}}},
    {"type": "function", "function": {"name": "task_blocked", "description": "Blocked", "parameters": {}}},
    {"type": "function", "function": {"name": "query_history", "description": "History", "parameters": {}}},
]


def _profile(**overrides: Any) -> dict[str, Any]:
    profile = {
        "profile_version": "ahp_v0",
        "task_understanding": {
            "summary": "Create a Python file and verify its output.",
            "important_properties": ["exact output", "hello.py"],
            "initial_working_theory": "Write the file, run it, and confirm the output.",
        },
        "solver_system_prompt": "Inspect first, then write hello.py and verify the exact output.",
        "context_configuration": {"preserve": ["task instruction"], "deprioritise": []},
        "context_pack_policy": {
            "include_sections": ["success_contract", "current_plan", "recent_steps"],
            "always_include": ["success_contract", "current_plan", "verifier_feedback"],
            "exclude_sections": [],
            "full_previous_steps": 4,
            "receipt_event_budget": 12,
            "failure_event_budget": 6,
            "tool_result_budget": 8,
            "verifier_feedback_budget": 3,
            "artifact_observation_budget": 5,
        },
        "tool_configuration": {
            "primary_tools": ["run_command", "read_file", "write_file", "task_done", "task_blocked"],
            "reserve_capabilities": [],
        },
        "success_definition": ["hello.py exists and prints Hello, World!"],
        "hard_visible_requirements": ["Create hello.py", "hello.py prints Hello, World!"],
        "inferred_success_requirements": ["python3 can run hello.py"],
        "verification_watchpoints": ["Check the exact output text."],
        "uncertain_or_exploratory_risks": [],
        "do_not_assume": ["Do not assume the newline behavior."],
        "verification_configuration": {
            "model_verifier_focus": ["Verify the exact output."],
            "required_final_evidence": ["command output from running hello.py"],
        },
        "repeat_action_guidance": "Do not repeat the same failed command without a changed hypothesis.",
        "approach_risks": ["Encoding drift"],
        "pivot_signals": ["Syntax error"],
        "initial_plan": [{"step": "Write hello.py", "status": "pending", "evidence_needed": "file exists"}],
        "compaction_recommendation": {"preserve": ["task instruction"], "deprioritise": []},
    }
    profile.update(overrides)
    return profile


def _profile_result(profile: dict[str, Any], *, warnings: list[str] | None = None) -> ProfileGenerationResult:
    return ProfileGenerationResult(
        profile=profile,
        profile_raw=json.dumps(profile),
        validation=ProfileValidationResult(valid=True, warnings=warnings or []),
        lint_findings=[],
        used_fallback=False,
        parse_succeeded=True,
        model_call_duration_sec=0.0,
        usage={},
    )


def _response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=(),
        usage={"cached_input_tokens": 0, "fresh_input_tokens": 0},
        status="completed",
        raw_response={},
    )


class _VerifierClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        self.calls.append(list(messages))
        return _response(
            json.dumps(
                {
                    "requirements": [
                        {
                            "requirement": "finish the task",
                            "verdict": "satisfied",
                            "evidence": "A replayed check succeeded.",
                            "evidence_refs": ["checks_results[0]"],
                        }
                    ],
                    "reason_codes": [],
                    "summary": "ok",
                }
            )
        )


class _FailingClient:
    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        raise RuntimeError("profile route unavailable")


def _prefix_digest(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _capture_verifier_payload(**kwargs: Any) -> dict[str, Any]:
    client = _VerifierClient()
    verify_fresh_context(
        "finish the task",
        {"cwd": "/workspace"},
        {"added_paths": []},
        {"summary": "done"},
        [CheckResult(command="pwd", exit_code=0, stdout="/workspace", stderr="", cwd="/workspace", duration_sec=0.01)],
        {"tool_calls": []},
        client,
        **kwargs,
    )
    assert client.calls
    return json.loads(client.calls[0][1]["content"])


def test_phase4_flag_off_keeps_prefix_digest_and_tools_identical() -> None:
    baseline_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _SAMPLE_TASK},
        {"role": "system", "content": "[orientation_snapshot]\n" + json.dumps(_SAMPLE_ORIENTATION, sort_keys=True, separators=(",", ":"), ensure_ascii=True)},
        {"role": "system", "content": "[tool_schemas]\n" + json.dumps(_SAMPLE_TOOL_SCHEMAS, sort_keys=True, separators=(",", ":"), ensure_ascii=True)},
    ]
    baseline_config = build_baseline_run_config(
        system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
    )

    ctx = ContextManager()
    ctx.build_prefix(
        system_prompt=baseline_config.system_prompt,
        task_instruction=_SAMPLE_TASK,
        orientation=_SAMPLE_ORIENTATION,
        tool_schemas=baseline_config.active_tool_schemas,
        frozen_success_contract=baseline_config.frozen_success_contract,
        extra_prefix_messages=baseline_config.extra_prefix_messages or None,
    )

    assert baseline_config.extra_prefix_messages == []
    assert baseline_config.completion_contract_items == []
    assert baseline_config.selected_tool_names == baseline_config.all_tool_names
    assert baseline_config.active_tool_schemas == _SAMPLE_TOOL_SCHEMAS
    assert ctx.digest_snapshot()["immutable_prefix_digest"] == _prefix_digest(baseline_messages)


def test_ahp_preflight_restores_the_green_gate() -> None:
    assert run_preflight() is True


def test_phase4_selected_tools_stay_within_base_tool_set() -> None:
    profile = _profile(
        tool_configuration={
            "primary_tools": ["run_command", "write_file", "task_done", "task_blocked"],
            "reserve_capabilities": [],
        }
    )
    run_config = apply_adaptation_contract(
        _profile_result(profile),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    assert set(run_config.selected_tool_names).issubset(set(run_config.all_tool_names))
    assert set(tool_names_from_schemas(run_config.active_tool_schemas)) == set(run_config.selected_tool_names)


def test_phase4_unknown_ahp_tool_names_are_ignored_and_warned() -> None:
    profile = _profile(
        tool_configuration={
            "primary_tools": ["run_command", "invented_tool", "task_done", "task_blocked"],
            "reserve_capabilities": [],
        }
    )
    available_tools = frozenset(tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS))
    validation = validate_profile(profile, available_tools)
    run_config = apply_adaptation_contract(
        _profile_result(profile, warnings=validation.warnings),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    assert "invented_tool" not in run_config.selected_tool_names
    assert any("unknown tool 'invented_tool'" in warning for warning in validation.warnings)


def test_unsupported_architect_config_fields_fail_clearly() -> None:
    profile = _profile(
        tool_policy={"route_solver_tools": "architect_owned"},
        helper_script_policy={"allow_generated_helpers": True},
        tool_configuration={
            "primary_tools": ["run_command", "read_file", "write_file", "task_done", "task_blocked"],
            "reserve_capabilities": [],
            "force_tool_routing": ["invented"],
        },
        verification_configuration={
            "model_verifier_focus": ["Verify output."],
            "required_final_evidence": ["fresh check"],
            "mutation_probe": "not allowed",
        },
    )

    validation = validate_profile(profile, frozenset(tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS)))

    assert validation.valid is False
    assert "unsupported profile field: helper_script_policy" in validation.errors
    assert "unsupported profile field: tool_policy" in validation.errors
    assert "unsupported tool_configuration field: force_tool_routing" in validation.errors
    assert "unsupported verification_configuration field: mutation_probe" in validation.errors


def test_phase4_ahp_generation_failure_surfaces_instead_of_fallback() -> None:
    try:
        generate_and_apply(
            task_instruction=_SAMPLE_TASK,
            orientation_dict=_SAMPLE_ORIENTATION,
            tool_catalogue=_SAMPLE_TOOL_SCHEMAS,
            model_client=_FailingClient(),
            available_tools=frozenset(tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS)),
            base_system_prompt=SYSTEM_PROMPT,
            base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
            base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        )
    except RuntimeError as exc:
        assert "profile route unavailable" in str(exc)
    else:
        raise AssertionError("AHP generation failure returned a run config instead of surfacing")


def test_phase4_unparseable_ahp_profile_surfaces_instead_of_fallback() -> None:
    class UnparseableClient:
        def __init__(self) -> None:
            self.responses = [_response("not json"), _response("still not json")]

        def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
            return self.responses.pop(0)

    try:
        generate_and_apply(
            task_instruction=_SAMPLE_TASK,
            orientation_dict=_SAMPLE_ORIENTATION,
            tool_catalogue=_SAMPLE_TOOL_SCHEMAS,
            model_client=UnparseableClient(),
            available_tools=frozenset(tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS)),
            base_system_prompt=SYSTEM_PROMPT,
            base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
            base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        )
    except AgentInitializationFailure as exc:
        assert "not valid JSON" in str(exc)
        assert exc.reason_code == "architect_config_json_invalid_after_retry"
    else:
        raise AssertionError("Unparseable AHP profile returned a run config instead of surfacing")


def test_phase4_schema_invalid_ahp_profile_gets_one_repair_attempt() -> None:
    incomplete = {
        "task_understanding": {
            "summary": "Create a Python file.",
            "important_properties": ["hello.py"],
            "initial_working_theory": "Write and run the file.",
        },
        "solver_system_prompt": "Write hello.py and verify the exact output.",
        "context_configuration": {"preserve": ["task"], "deprioritise": []},
        "tool_configuration": {
            "primary_tools": ["run_command", "read_file", "write_file", "task_done", "task_blocked"],
            "reserve_capabilities": [],
        },
        "hard_visible_requirements": ["Create hello.py"],
        "inferred_success_requirements": ["python3 can run hello.py"],
        "verification_watchpoints": ["Check exact output."],
        "pivot_signals": ["Syntax error"],
        "initial_plan": [{"step": "Write hello.py", "status": "pending", "evidence_needed": "file exists"}],
    }
    repaired = _profile()

    class RepairingClient:
        def __init__(self) -> None:
            self.responses = [_response(json.dumps(incomplete)), _response(json.dumps(repaired))]
            self.calls: list[list[dict[str, Any]]] = []

        def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
            self.calls.append(list(messages))
            return self.responses.pop(0)

    client = RepairingClient()
    run_config = generate_and_apply(
        task_instruction=_SAMPLE_TASK,
        orientation_dict=_SAMPLE_ORIENTATION,
        tool_catalogue=_SAMPLE_TOOL_SCHEMAS,
        model_client=client,
        available_tools=frozenset(tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS)),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
    )

    assert len(client.calls) == 2
    assert "failed schema validation" in client.calls[1][1]["content"]
    assert run_config.profile_result is not None
    assert run_config.profile_result.used_fallback is False
    assert run_config.profile_result.validation.valid is True
    assert run_config.completion.hard_requirements == tuple(repaired["hard_visible_requirements"])


def test_schema_invalid_ahp_profile_fails_as_agent_initialization_after_one_repair_attempt() -> None:
    incomplete = {
        "task_understanding": {"summary": "Create a Python file."},
        "solver_system_prompt": "Write hello.py and verify the exact output.",
        "tool_configuration": {"primary_tools": ["run_command", "task_done", "task_blocked"]},
    }

    class StillInvalidClient:
        def __init__(self) -> None:
            self.responses = [_response(json.dumps(incomplete)), _response(json.dumps(incomplete))]
            self.calls: list[list[dict[str, Any]]] = []

        def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
            self.calls.append(list(messages))
            return self.responses.pop(0)

    client = StillInvalidClient()
    try:
        generate_and_apply(
            task_instruction=_SAMPLE_TASK,
            orientation_dict=_SAMPLE_ORIENTATION,
            tool_catalogue=_SAMPLE_TOOL_SCHEMAS,
            model_client=client,
            available_tools=frozenset(tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS)),
            base_system_prompt=MECHANICAL_SYSTEM_PROMPT,
            base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
            base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
            use_full_generated_prompt=True,
        )
    except AgentInitializationFailure as exc:
        assert exc.reason_code == "architect_config_schema_invalid_after_retry"
        assert "failed validation" in str(exc)
    else:
        raise AssertionError("schema-invalid AHP config returned a run config instead of agent initialization failure")
    assert len(client.calls) == 2


def test_phase4_hard_visible_requirements_seed_completion_and_verifier() -> None:
    run_config = apply_adaptation_contract(
        _profile_result(_profile(hard_visible_requirements=["Create hello.py", "Print Hello, World!"])),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    assert run_config.completion.hard_requirements == ("Create hello.py", "Print Hello, World!")
    assert run_config.verifier.hard_requirements == ("Create hello.py", "Print Hello, World!")
    assert "[hard_visible_requirements]" in run_config.verifier.render_contract_text("base contract")


def test_receipt_context_pack_policy_is_validated_and_clamped() -> None:
    policy = validate_context_pack_policy(
        {
            "include_sections": ["success_contract", "private_reasoning", "recent_steps", "made_up"],
            "always_include": ["raw_full_transcript", "verifier_feedback"],
            "exclude_sections": ["external_history"],
            "full_previous_steps": 99,
            "receipt_event_budget": 99,
            "failure_event_budget": 99,
            "tool_result_budget": 99,
            "verifier_feedback_budget": 99,
            "artifact_observation_budget": 99,
        }
    )

    assert policy.include_sections == (
        "success_contract",
        "recent_steps",
        "artifact_observations",
        "current_plan",
        "evidence_refs",
        "recent_failures",
        "verifier_feedback",
    )
    assert policy.always_include == (
        "verifier_feedback",
        "artifact_observations",
        "current_plan",
        "evidence_refs",
        "recent_failures",
        "recent_steps",
    )
    assert policy.exclude_sections == ()
    assert policy.full_previous_steps == 8
    assert policy.receipt_event_budget == 30
    assert policy.failure_event_budget == 12
    assert policy.tool_result_budget == 20
    assert policy.verifier_feedback_budget == 5
    assert policy.artifact_observation_budget == 10


def test_architect_context_policy_cannot_remove_recent_evidence_invariants() -> None:
    policy = validate_context_pack_policy(
        {
            "include_sections": ["success_contract"],
            "always_include": ["success_contract"],
            "exclude_sections": [
                "recent_steps",
                "recent_failures",
                "verifier_feedback",
                "artifact_observations",
                "evidence_refs",
            ],
            "receipt_event_budget": 0,
            "failure_event_budget": 0,
            "tool_result_budget": 0,
            "verifier_feedback_budget": 0,
            "artifact_observation_budget": 0,
        }
    )

    for section in INVARIANT_CONTEXT_PACK_SECTIONS:
        assert section in policy.include_sections
        assert section in policy.always_include
        assert section not in policy.exclude_sections
    assert policy.receipt_event_budget >= 6
    assert policy.failure_event_budget >= 1
    assert policy.tool_result_budget >= 2
    assert policy.verifier_feedback_budget >= 1
    assert policy.artifact_observation_budget >= 1


def test_ahp_full_prompt_variant_composes_kernel_and_drops_task_block_prefix() -> None:
    profile = _profile(solver_system_prompt="Task-specific solver kernel.")
    run_config = apply_adaptation_contract(
        _profile_result(profile),
        base_system_prompt=MECHANICAL_SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
        use_full_generated_prompt=True,
    )

    assert run_config.system_prompt.startswith(MECHANICAL_SYSTEM_PROMPT)
    assert "[architect_solver_prompt]" in run_config.system_prompt
    assert "Task-specific solver kernel." in run_config.system_prompt
    assert SYSTEM_PROMPT not in run_config.system_prompt
    assert run_config.task_block == "Task-specific solver kernel."
    assert all("[ahp_task_block]" not in message["content"] for message in run_config.extra_prefix_messages)


def test_architect_solver_prompt_competes_with_no_static_behavior_prompt_when_supplied() -> None:
    architect_prompt = "Architect-owned solver instructions: inspect the manifest, repair one file, and verify via command output."
    run_config = apply_adaptation_contract(
        _profile_result(_profile(solver_system_prompt=architect_prompt)),
        base_system_prompt=MECHANICAL_SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
        use_full_generated_prompt=True,
    )

    assert run_config.system_prompt.startswith(MECHANICAL_SYSTEM_PROMPT)
    assert "[architect_solver_prompt]" in run_config.system_prompt
    assert architect_prompt in run_config.system_prompt
    assert SYSTEM_PROMPT not in run_config.system_prompt
    assert "Default working loop" not in run_config.system_prompt
    assert "Your job is to solve the task" not in run_config.system_prompt
    assert not any("[ahp_task_block]" in str(message.get("content", "")) for message in run_config.extra_prefix_messages)


def test_architect_verifier_prompt_reaches_run_config_and_verify_call() -> None:
    verifier_prompt = "Architect-owned verifier instructions: inspect exact output evidence and reject proxy claims."
    run_config = apply_adaptation_contract(
        _profile_result(
            _profile(
                verification_configuration={
                    "verifier_system_prompt": verifier_prompt,
                    "model_verifier_focus": ["Verify exact output."],
                    "required_final_evidence": ["command output from running hello.py"],
                }
            )
        ),
        base_system_prompt=MECHANICAL_SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
        use_full_generated_prompt=True,
    )

    assert run_config.verifier_system_prompt == verifier_prompt

    client = _VerifierClient()
    verify_fresh_context(
        "finish the task",
        {"cwd": "/workspace"},
        {"added_paths": []},
        {"summary": "done"},
        [CheckResult(command="pwd", exit_code=0, stdout="/workspace", stderr="", cwd="/workspace", duration_sec=0.01)],
        {"tool_calls": []},
        client,
        verifier_system_prompt=run_config.verifier_system_prompt,
    )

    system_message = client.calls[0][0]["content"]
    assert "[architect_verifier_prompt]" in system_message
    assert verifier_prompt in system_message
    assert "[harness_verifier_schema_contract]" in system_message


def test_phase4_ahp_uses_two_verifier_rounds_without_changing_baseline_default() -> None:
    baseline = build_baseline_run_config(
        system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
    )
    run_config = apply_adaptation_contract(
        _profile_result(_profile()),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    assert baseline.verifier.max_rounds == 1
    assert run_config.verifier.max_rounds == 2


def test_ahp_prefix_preserves_stable_test_runner_evidence_contract() -> None:
    run_config = apply_adaptation_contract(
        _profile_result(_profile()),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    prefix_text = "\n".join(str(msg.get("content", "")) for msg in run_config.extra_prefix_messages)

    assert "[ahp_evidence_contract]" in prefix_text
    assert "pytest-style" in prefix_text
    assert "python test_file.py" in prefix_text


def test_phase4_inferred_requirements_are_tagged_but_do_not_seed_hard_completion() -> None:
    run_config = apply_adaptation_contract(
        _profile_result(_profile(inferred_success_requirements=["python3 can run hello.py"])),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    assert "[inferred] python3 can run hello.py" in run_config.verifier_stated_requirements
    assert "python3 can run hello.py" not in run_config.completion_contract_items
    assert "python3 can run hello.py" not in run_config.verifier.stated_requirements_for_ledger()


def test_phase4_verifier_focus_reaches_verify_fresh_context() -> None:
    payload = _capture_verifier_payload(verifier_focus=["Check the exact visible output."])

    assert payload["verifier_policy"]["focus"] == ["Check the exact visible output."]


def test_phase4_do_not_assume_reaches_verify_fresh_context() -> None:
    payload = _capture_verifier_payload(verifier_do_not_assume=["Do not assume hidden files are readable."])

    assert payload["verifier_policy"]["do_not_assume"] == ["Do not assume hidden files are readable."]


def test_phase4_required_final_evidence_reaches_completion_contract_and_verifier_payload() -> None:
    run_config = make_harness_run_config(
        system_prompt=SYSTEM_PROMPT,
        active_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        selected_tool_names=tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS),
        all_tool_names=tool_names_from_schemas(_SAMPLE_TOOL_SCHEMAS),
        base_requirements=["finish the task"],
        required_final_evidence=(" command output ", "command output", "artifact readback"),
    )

    contract = _build_completion_contract("finish the task", {}, completion_policy=run_config.completion)
    payload = _capture_verifier_payload(
        required_final_evidence=list(run_config.completion.required_final_evidence)
    )

    assert contract["required_final_evidence"] == ["command output", "artifact readback"]
    assert payload["verifier_policy"]["required_final_evidence"] == ["command output", "artifact readback"]


def test_phase4_repeat_guidance_surfaces_in_blind_retry_envelope(tmp_path: Path) -> None:
    envelope = _build_blind_retry_blocked_envelope(
        "run_command",
        {"cmd": "false"},
        str(tmp_path),
        raw_log_dir=tmp_path / "raw_logs",
        guidance="Inspect the failure and change state before retrying the same command.",
    )

    assert envelope.blind_retry_blocked is True
    assert "Inspect the failure and change state before retrying the same command." in envelope.stderr_head


def test_phase4_ahp_artifacts_still_written(tmp_path: Path) -> None:
    run_config = apply_adaptation_contract(
        _profile_result(_profile()),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    paths = write_ahp_artifacts(tmp_path / "ahp", run_config)

    assert {
        "adaptation_contract",
        "adaptation_contract_raw",
        "validation",
        "generated_task_block",
        "selected_tools",
        "completion_contract",
        "verifier_payload_preview",
        "authority_mapping",
        "validated_run_config",
        "config_realization_audit",
    }.issubset(paths)
    for path_str in paths.values():
        path = Path(path_str)
        assert path.exists()
        assert path.stat().st_size > 0
    audit = json.loads(Path(paths["config_realization_audit"]).read_text(encoding="utf-8"))
    assert audit["status"] == "realized"
    assert audit["realized_fields"]["solver_system_prompt"] == "system_prompt/task_block"
    assert "recent_steps" in audit["context_pack_invariants"]


def test_config_realization_audit_lists_realized_and_rejected_surfaces() -> None:
    validation = ProfileValidationResult(
        valid=True,
        warnings=["tool_configuration.primary_tools contains unknown tool 'invented' - move to reserve_capabilities"],
    )
    run_config = apply_adaptation_contract(
        _profile_result(
            _profile(
                tool_configuration={
                    "primary_tools": ["run_command", "read_file", "write_file", "task_done", "task_blocked"],
                    "reserve_capabilities": ["domain-specific visual inspection"],
                }
            ),
            warnings=validation.warnings,
        ),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    audit = build_config_realization_audit(run_config)

    assert audit["realized_fields"]["tool_configuration.reserve_capabilities"] == "tools.reserve_capabilities metadata"
    assert audit["reserve_capabilities"] == ["domain-specific visual inspection"]
    assert audit["rejected_or_unsupported"] == validation.warnings


def test_phase6_advisory_ahp_fields_reach_profile_summary_context() -> None:
    run_config = apply_adaptation_contract(
        _profile_result(
            _profile(
                tool_configuration={
                    "primary_tools": ["run_command", "read_file", "write_file", "task_done", "task_blocked"],
                    "reserve_capabilities": ["query_evidence for prior command output"],
                },
                success_definition=["hello.py prints the required text"],
                uncertain_or_exploratory_risks=["package layout may be unusual"],
                context_configuration={
                    "preserve": ["exact requested output"],
                    "deprioritise": ["irrelevant repository files"],
                },
                compaction_recommendation={
                    "preserve": ["verification evidence"],
                    "deprioritise": ["long install logs"],
                },
            )
        ),
        base_system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
    )

    profile_messages = [
        message for message in run_config.extra_prefix_messages
        if str(message.get("content", "")).startswith("[ahp_profile_summary]\n")
    ]
    assert len(profile_messages) == 1
    summary = json.loads(profile_messages[0]["content"].split("\n", 1)[1])
    assert summary["reserve_capabilities"] == ["query_evidence for prior command output"]
    assert summary["success_definition"] == ["hello.py prints the required text"]
    assert summary["uncertain_or_exploratory_risks"] == ["package layout may be unusual"]
    assert summary["context_preserve"] == ["exact requested output"]
    assert summary["context_deprioritise"] == ["irrelevant repository files"]
    assert summary["compaction_preserve"] == ["verification evidence"]
    assert summary["compaction_deprioritise"] == ["long install logs"]
