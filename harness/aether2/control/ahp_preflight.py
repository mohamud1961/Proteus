"""AHP Stage 2C preflight: proves flag-off baseline identity and flag-on correctness."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from harness.aether2.runtime.adaptive_context import (
    ValidatedRunConfig,
    _extract_initial_plan,
    _render_initial_plan_checklist,
    apply_adaptation_contract,
    generate_and_apply,
)
from harness.aether2.runtime.adaptive_artifacts import write_ahp_artifacts
from harness.aether2.runtime.adaptive_profile import (
    ProfileGenerationResult,
    ProfileValidationResult,
    validate_profile,
)
from harness.aether2.runtime.adaptive_profile_helpers import (
    solver_visible_orientation,
)
from harness.aether2.runtime.context import ContextManager
from harness.aether2.runtime.prompts import MECHANICAL_SYSTEM_PROMPT, SYSTEM_PROMPT
from harness.aether2.control.ahp_startup import (
    _baseline_run_config,
    _compute_baseline_diff,
    run_ahp_startup,
)


# --- Test fixtures ---

_SAMPLE_TASK = "Create a Python file hello.py that prints 'Hello, World!' when run."

_SAMPLE_ORIENTATION: dict[str, Any] = {
    "cwd": "/workspace",
    "user": "agent",
    "workspace_root": "/workspace",
    "writable_paths": ["/workspace"],
    "safe_file_listing": ["README.md", "setup.py"],
    "tool_presence": {"python3": "/usr/bin/python3", "git": "/usr/bin/git"},
    "package_managers": {"pip": "/usr/bin/pip"},
    "network": "reachable",
    "runtimes": {"python3": "Python 3.11.0"},
    "processes": [],
    "ports": [],
}

_SAMPLE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"function": {"name": "run_command", "description": "Run a shell command", "parameters": {}}},
    {"function": {"name": "read_file", "description": "Read a file", "parameters": {}}},
    {"function": {"name": "write_file", "description": "Write a file", "parameters": {}}},
    {"function": {"name": "task_done", "description": "Signal task completion", "parameters": {}}},
    {"function": {"name": "task_blocked", "description": "Signal task is blocked", "parameters": {}}},
    {"function": {"name": "start_job", "description": "Start a background job", "parameters": {}}},
    {"function": {"name": "job_status", "description": "Check job status", "parameters": {}}},
    {"function": {"name": "query_evidence", "description": "Search current-run evidence", "parameters": {}}},
]

_SAMPLE_STATED_REQUIREMENTS = [
    "Create hello.py",
    "Print 'Hello, World!' when run",
]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _build_baseline_prefix(
    orientation: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
) -> tuple[str, str, str]:
    """Build a baseline prefix and return (prefix_digest, tool_digest, frozen_bytes_hex)."""
    ctx = ContextManager(delta_state=None)
    prefix = ctx.build_prefix(
        system_prompt=SYSTEM_PROMPT,
        task_instruction=_SAMPLE_TASK,
        orientation=orientation,
        tool_schemas=tool_schemas,
    )
    digests = ctx.digest_snapshot()
    return (
        digests["immutable_prefix_digest"],
        digests["tool_schema_digest"],
        hashlib.sha256(prefix.frozen_bytes).hexdigest(),
    )


class PreflightResult:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, evidence: str) -> None:
        self.checks.append((name, passed, evidence))
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}: {evidence}")

    @property
    def all_passed(self) -> bool:
        return all(passed for _, passed, _ in self.checks)


def preflight_flag_off(result: PreflightResult) -> None:
    """Prove flag-off baseline is byte-identical."""
    print("\n=== FLAG OFF BASELINE IDENTITY ===")

    # Build baseline prefix
    baseline_prefix_digest, baseline_tool_digest, baseline_frozen_hex = _build_baseline_prefix(
        _SAMPLE_ORIENTATION, _SAMPLE_TOOL_SCHEMAS,
    )

    # Build a "flag-off" config -- this is what loop.py uses when adaptive_profile_enabled=False
    baseline_config = _baseline_run_config(_SAMPLE_TOOL_SCHEMAS, _SAMPLE_STATED_REQUIREMENTS)

    # Build prefix using baseline config (simulating the flag-off path)
    ctx_off = ContextManager(delta_state=None)
    prefix_off = ctx_off.build_prefix(
        system_prompt=baseline_config.system_prompt,
        task_instruction=_SAMPLE_TASK,
        orientation=_SAMPLE_ORIENTATION,
        tool_schemas=baseline_config.active_tool_schemas,
        frozen_success_contract=baseline_config.frozen_success_contract,
        extra_prefix_messages=baseline_config.extra_prefix_messages or None,
    )
    digests_off = ctx_off.digest_snapshot()

    # 1. Prefix digest identical
    result.check(
        "prefix_digest_identical",
        digests_off["immutable_prefix_digest"] == baseline_prefix_digest,
        f"baseline={baseline_prefix_digest[:16]}... off={digests_off['immutable_prefix_digest'][:16]}...",
    )

    # 2. Frozen bytes identical
    off_frozen_hex = hashlib.sha256(prefix_off.frozen_bytes).hexdigest()
    result.check(
        "frozen_bytes_identical",
        off_frozen_hex == baseline_frozen_hex,
        f"baseline={baseline_frozen_hex[:16]}... off={off_frozen_hex[:16]}...",
    )

    # 3. Tool schema set identical
    result.check(
        "tool_schema_digest_identical",
        digests_off["tool_schema_digest"] == baseline_tool_digest,
        f"baseline={baseline_tool_digest[:16]}... off={digests_off['tool_schema_digest'][:16]}...",
    )

    # 4. System prompt identical
    result.check(
        "system_prompt_identical",
        baseline_config.system_prompt == SYSTEM_PROMPT,
        f"len(baseline)={len(baseline_config.system_prompt)}, len(SYSTEM_PROMPT)={len(SYSTEM_PROMPT)}",
    )

    # 5. Completion contract empty (baseline has no AHP items)
    result.check(
        "completion_contract_empty",
        baseline_config.completion_contract_items == [],
        f"items={baseline_config.completion_contract_items}",
    )

    # 6. No frozen success contract
    result.check(
        "no_frozen_success_contract",
        baseline_config.frozen_success_contract is None,
        f"contract={baseline_config.frozen_success_contract}",
    )

    # 7. No extra prefix messages
    result.check(
        "no_extra_prefix_messages",
        baseline_config.extra_prefix_messages == [],
        f"messages={len(baseline_config.extra_prefix_messages)}",
    )

    # 8. Initial plan empty
    result.check(
        "initial_plan_empty",
        len(baseline_config.initial_plan) == 0,
        f"plan={baseline_config.initial_plan}",
    )

    # 9. Baseline diff report shows all IDENTICAL
    diff = _compute_baseline_diff(baseline_config, baseline_config)
    all_identical = all("IDENTICAL" in line or "===" in line or "used_fallback" in line or "fallback_reason" in line for line in diff.split("\n"))
    result.check(
        "baseline_diff_all_identical",
        all_identical,
        f"diff lines with IDENTICAL: {diff.count('IDENTICAL')}/7",
    )


def preflight_flag_on_failure_surfaces(result: PreflightResult) -> None:
    """Prove flag-on AHP failures surface instead of falling back."""
    print("\n=== FLAG ON: FAILURE SURFACES ===")

    class FailingClient:
        def call(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("synthetic profile failure")

    available_tools = frozenset(
        (s.get("function", s)).get("name", "") for s in _SAMPLE_TOOL_SCHEMAS
    )
    try:
        generate_and_apply(
            task_instruction=_SAMPLE_TASK,
            orientation_dict=solver_visible_orientation(_SAMPLE_ORIENTATION),
            tool_catalogue=list(_SAMPLE_TOOL_SCHEMAS),
            model_client=FailingClient(),
            available_tools=available_tools,
            base_system_prompt=SYSTEM_PROMPT,
            base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
            base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        )
    except RuntimeError as exc:
        result.check(
            "ahp_generation_failure_surfaces",
            "synthetic profile failure" in str(exc),
            f"error={exc}",
        )
    else:
        result.check(
            "ahp_generation_failure_surfaces",
            False,
            "generate_and_apply returned a config instead of raising",
        )


def preflight_flag_on_synthetic(result: PreflightResult) -> None:
    """Prove flag-on with a synthetic (non-model) profile works end-to-end."""
    print("\n=== FLAG ON: SYNTHETIC PROFILE (NO MODEL CALL) ===")

    available_tools = frozenset(
        (s.get("function", s)).get("name", "") for s in _SAMPLE_TOOL_SCHEMAS
    )

    # Build a synthetic profile that would come from a model
    synthetic_profile: dict[str, Any] = {
        "profile_version": "ahp_v0",
        "task_understanding": {
            "summary": "Create a Python file that prints a greeting.",
            "important_properties": ["exact output string", "file name hello.py"],
            "initial_working_theory": "Write the file, run it, verify output.",
        },
        "solver_system_prompt": (
            "You are solving a file creation task. Write the file, verify it runs "
            "and produces the exact expected output. Do not skip verification. "
            "Do not repeat failed approaches without changing state."
        ),
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
            "primary_tools": ["run_command", "write_file", "read_file", "task_done", "task_blocked"],
            "reserve_capabilities": [],
        },
        "success_definition": ["hello.py exists and prints 'Hello, World!'"],
        "hard_visible_requirements": [
            "File hello.py must exist",
            "Running python3 hello.py must print 'Hello, World!'",
        ],
        "inferred_success_requirements": [
            "File should be executable with python3",
        ],
        "verification_watchpoints": [
            "Check exact output string including newline",
            "Check file is valid Python",
        ],
        "uncertain_or_exploratory_risks": [],
        "do_not_assume": [
            "exact trailing newline behavior",
        ],
        "verification_configuration": {
            "model_verifier_focus": ["Does the output match exactly?"],
            "required_final_evidence": ["command output from running hello.py"],
        },
        "repeat_action_guidance": "Do not re-write hello.py without checking what went wrong.",
        "approach_risks": ["Encoding issues", "Wrong python version"],
        "pivot_signals": ["Import error", "Syntax error"],
        "initial_plan": [
            {"step": "Write hello.py with print statement", "status": "pending", "evidence_needed": "file written"},
            {"step": "Run python3 hello.py and check output", "status": "pending", "evidence_needed": "stdout matches"},
            {"step": "Call task_done with evidence", "status": "pending", "evidence_needed": "verification complete"},
        ],
        "compaction_recommendation": {"preserve": ["task instruction"], "deprioritise": []},
    }

    # Validate
    validation = validate_profile(synthetic_profile, available_tools)
    result.check(
        "synthetic_profile_validates",
        validation.valid,
        f"valid={validation.valid}, errors={validation.errors}, warnings={validation.warnings}",
    )

    # Create ProfileGenerationResult
    profile_result = ProfileGenerationResult(
        profile=synthetic_profile,
        profile_raw=json.dumps(synthetic_profile),
        validation=ProfileValidationResult(valid=True),
        lint_findings=[],
        used_fallback=False,
        parse_succeeded=True,
        model_call_duration_sec=0.0,
        usage={},
    )

    # Apply
    run_config = apply_adaptation_contract(
        profile_result,
        base_system_prompt=MECHANICAL_SYSTEM_PROMPT,
        base_tool_schemas=_SAMPLE_TOOL_SCHEMAS,
        base_stated_requirements=_SAMPLE_STATED_REQUIREMENTS,
        task_instruction=_SAMPLE_TASK,
        use_full_generated_prompt=True,
    )

    # 1. System prompt = mechanical frame + architect-owned solver prompt
    result.check(
        "system_prompt_uses_mechanical_frame",
        run_config.system_prompt.startswith(MECHANICAL_SYSTEM_PROMPT),
        f"len(prompt)={len(run_config.system_prompt)}, len(mechanical)={len(MECHANICAL_SYSTEM_PROMPT)}",
    )
    result.check(
        "architect_solver_prompt_in_system_prompt",
        "[architect_solver_prompt]" in run_config.system_prompt and synthetic_profile["solver_system_prompt"] in run_config.system_prompt,
        f"task_block_len={len(run_config.task_block)}, in_system={'[architect_solver_prompt]' in run_config.system_prompt}",
    )
    result.check(
        "static_behavioral_prompt_not_competing",
        SYSTEM_PROMPT not in run_config.system_prompt,
        f"legacy_prompt_present={SYSTEM_PROMPT in run_config.system_prompt}",
    )

    # 1b. Task block is not duplicated in the frozen context pack.
    task_block_in_prefix = any(
        "[ahp_task_block]" in msg.get("content", "")
        for msg in run_config.extra_prefix_messages
    )
    result.check(
        "task_block_not_duplicated_in_context_pack",
        not task_block_in_prefix and len(run_config.task_block) > 0,
        f"task_block_len={len(run_config.task_block)}, in_prefix={task_block_in_prefix}",
    )

    # 2. Selected tools exposed, unselected hidden
    selected = set(run_config.selected_tool_names)
    hidden = set(run_config.all_tool_names) - selected
    result.check(
        "tools_selected",
        "run_command" in selected and "write_file" in selected,
        f"selected={sorted(selected)}",
    )
    result.check(
        "unselected_hidden",
        len(hidden) > 0,
        f"hidden={sorted(hidden)}",
    )

    # 3. Completion contract gets hard_visible_requirements
    result.check(
        "completion_contract_has_hard_reqs",
        len(run_config.completion_contract_items) == 2,
        f"items={run_config.completion_contract_items}",
    )

    # 4. Verifier receives authority-tagged stated_requirements
    inferred_items = [r for r in run_config.verifier_stated_requirements if r.startswith("[inferred]")]
    result.check(
        "verifier_has_inferred_reqs",
        len(inferred_items) > 0,
        f"inferred_count={len(inferred_items)}",
    )

    # 5. Initial plan renders as checklist
    plan_text = _render_initial_plan_checklist(run_config.initial_plan)
    result.check(
        "initial_plan_renders",
        "[initial_plan]" in plan_text and "[ ]" in plan_text,
        f"plan_len={len(plan_text)}, has_checkboxes={'[ ]' in plan_text}",
    )
    result.check(
        "initial_plan_revisable_note",
        "not a script" in plan_text,
        f"contains_revisable_note={'not a script' in plan_text}",
    )

    # 6. Extra prefix messages = frozen context pack (summary + plan)
    result.check(
        "extra_prefix_messages_present",
        len(run_config.extra_prefix_messages) >= 3,
        f"count={len(run_config.extra_prefix_messages)}",
    )
    # Profile summary follows the stable evidence contract.
    summary_msg = run_config.extra_prefix_messages[1]["content"]
    result.check(
        "profile_summary_in_prefix",
        "[ahp_profile_summary]" in summary_msg,
        f"has_tag={'[ahp_profile_summary]' in summary_msg}",
    )
    # Plan content frozen in context pack.
    plan_msg = run_config.extra_prefix_messages[2]["content"]
    result.check(
        "plan_content_frozen_in_pack",
        "[initial_plan]" in plan_msg and "[ ]" in plan_msg,
        f"has_plan={'[initial_plan]' in plan_msg}",
    )

    # 7. Frozen success contract present
    result.check(
        "frozen_success_contract_present",
        run_config.frozen_success_contract is not None,
        f"has_contract={run_config.frozen_success_contract is not None}",
    )

    # 8. Write artifacts to temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        artifacts_dir = Path(tmpdir)
        paths = write_ahp_artifacts(artifacts_dir, run_config)
        expected_artifacts = {
            "adaptation_contract", "adaptation_contract_raw", "validation",
            "generated_task_block", "selected_tools", "completion_contract",
            "verifier_payload_preview", "authority_mapping", "validated_run_config",
        }
        written = set(paths.keys())
        result.check(
            "artifacts_written",
            expected_artifacts.issubset(written),
            f"written={sorted(written)}, expected={sorted(expected_artifacts)}, missing={sorted(expected_artifacts - written)}",
        )
        # Verify artifacts are real files with content
        for name, path_str in paths.items():
            p = Path(path_str)
            if not p.exists() or p.stat().st_size == 0:
                result.check(f"artifact_{name}_real", False, f"path={path_str} exists={p.exists()}")
                break
        else:
            result.check(
                "all_artifacts_real_files",
                True,
                f"all {len(paths)} artifacts are non-empty files",
            )

    # 9. Baseline diff shows differences (since we have an active profile)
    baseline = _baseline_run_config(_SAMPLE_TOOL_SCHEMAS, _SAMPLE_STATED_REQUIREMENTS)
    diff = _compute_baseline_diff(baseline, run_config)
    result.check(
        "baseline_diff_shows_changes",
        "DIFFERS" in diff,
        f"diff_has_DIFFERS={'DIFFERS' in diff}",
    )

    # 10. Verify do_not_assume reaches verifier
    result.check(
        "do_not_assume_in_verifier",
        len(run_config.verifier_do_not_assume) > 0,
        f"count={len(run_config.verifier_do_not_assume)}",
    )

    # 11. Verifier focus from watchpoints
    result.check(
        "verifier_focus_from_watchpoints",
        len(run_config.verifier_focus) > 0,
        f"count={len(run_config.verifier_focus)}",
    )

    # 12. Plan structure: content frozen in pack, status is a separate rendering concern
    #     _render_initial_plan_checklist is the status renderer (dynamic tail path).
    #     The frozen pack has the CONTENT; status updates happen per-turn in tail.
    #     Verify the structure: plan data in initial_plan, content text in pack,
    #     and the render function produces per-turn status text.
    status_updated_plan = [
        {**step, "status": "done"} for step in run_config.initial_plan
    ]
    status_render = _render_initial_plan_checklist(status_updated_plan)
    result.check(
        "plan_status_renders_separately",
        "[x]" in status_render and "[x]" not in plan_msg,
        f"tail_has_done_marker={'[x]' in status_render}, pack_has_no_done={'[x]' not in plan_msg}",
    )


def preflight_neutral_wording(result: PreflightResult) -> None:
    """Verify no benchmark-implying language in the schema or configurator."""
    print("\n=== NEUTRAL WORDING CHECK ===")
    from harness.aether2.runtime import adaptive_profile, adaptive_context, adaptive_profile_helpers

    # Terms banned from model-visible prompts/schema (denylist constants are OK)
    banned_terms = ["likely_failure_modes", "likely_traps", "decoy"]
    # hidden_tests is allowed only inside GRADER_LEAK_KEYS (the safety denylist)
    for mod_name, mod in [
        ("adaptive_profile", adaptive_profile),
        ("adaptive_context", adaptive_context),
        ("adaptive_profile_helpers", adaptive_profile_helpers),
    ]:
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for term in banned_terms:
            found = term in src
            result.check(
                f"no_{term}_in_{mod_name}",
                not found,
                f"'{term}' {'FOUND' if found else 'absent'} in {mod_name}",
            )

    # Verify approach_risks replaced likely_failure_modes
    result.check(
        "approach_risks_in_schema",
        "approach_risks" in adaptive_profile.REQUIRED_PROFILE_FIELDS,
        f"fields={sorted(adaptive_profile.REQUIRED_PROFILE_FIELDS)}",
    )


def run_preflight() -> bool:
    """Run all preflight checks. Returns True if all pass."""
    result = PreflightResult()

    preflight_flag_off(result)
    preflight_flag_on_failure_surfaces(result)
    preflight_flag_on_synthetic(result)
    preflight_neutral_wording(result)

    print(f"\n{'='*50}")
    passed = sum(1 for _, p, _ in result.checks if p)
    total = len(result.checks)
    print(f"PREFLIGHT: {passed}/{total} checks passed")
    if not result.all_passed:
        print("FAILED checks:")
        for name, passed_flag, evidence in result.checks:
            if not passed_flag:
                print(f"  FAIL: {name} -- {evidence}")
    return result.all_passed


if __name__ == "__main__":
    success = run_preflight()
    sys.exit(0 if success else 1)
