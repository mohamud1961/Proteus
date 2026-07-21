"""Completion-evidence protocol: content-blind gate on completed verdicts.

Design of record: audit addendum (FABLE5_ADVERSARIAL_AUDIT_20260708T165639Z.md)
Concern 1 — the harness checks presence, non-emptiness, and that
inspection_refs resolve to inspections actually performed in the round.
It never evaluates reasoning content.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.model_hooks import (
    ModelHooks,
    VERIFIER_RUNTIME_CONTRACT,
    _completion_independence_problem,
    _completion_record_problem,
    _independent_derivation_refs,
    _refs_from_inspections,
)
from aether_next.runtime_ir import CapabilityDescriptor, EnvMap, RuntimeConfigIR
from aether_next.proof_contract import ROUTE_EVIDENCE_CEILINGS
from aether_next.verifier import (
    CompletionEvidenceEntry,
    CompletionEvidenceShapeError,
    parse_model_verifier_result,
)
from aether_next.verifier_inspector import VerifierInspectionRequest
from aether_next.workbench_compile import harness_config_to_runtime_ir
from aether_next.workbench_config import parse_harness_config_ir
from aether_next.workbench_prompt import WORKBENCH_ARCHITECT_SYSTEM_PROMPT


def _make_envmap(**overrides: Any) -> EnvMap:
    defaults: dict[str, Any] = {
        "task_prompt": "Write out.txt with the decoded value.",
        "workspace_root": "/app",
        "visible_files": ("out.txt",),
        "capabilities": {
            "shell": CapabilityDescriptor(capability_id="shell", summary="run commands"),
            "filesystem": CapabilityDescriptor(capability_id="filesystem", summary="read/write files"),
        },
    }
    defaults.update(overrides)
    return EnvMap(**defaults)


def _compiled():
    envmap = _make_envmap()
    return ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
        RuntimeConfigIR(
            architect_summary="summary",
            solver_identity_prompt="solver",
            selected_capabilities=("shell", "filesystem"),
            verifier_identity_prompt="Task-specific verifier prompt.",
        ),
        envmap,
    )


def _ledger_stub():
    return type("_L", (), {"all_receipts": lambda self: []})()


_INSPECT_REQUEST = json.dumps({
    "kind": "inspect",
    "summary": "Read the decisive output transcript.",
    "requests": [
        {"request_id": "probe-1", "kind": "read_output", "handle": "5:a-1:stdout", "span": 4000}
    ],
})


def _inspection_id(request_id: str) -> str:
    return f"inspection:test:{request_id}"


def _grounded_inspector(requests):
    return [
        {
            "request_id": req.request_id,
            "inspection_id": _inspection_id(req.request_id),
            "kind": req.kind,
            "handle": req.handle,
            "path": req.path,
            "excerpt": "observed value=7 matches requirement value=7",
            "eligible_for_proof": True,
            "evidence_ceiling": ROUTE_EVIDENCE_CEILINGS[req.kind],
        }
        for req in requests
    ]


def _completed(record: list[dict[str, Any]] | None) -> str:
    payload: dict[str, Any] = {
        "verdict": "completed",
        "confidence": "high",
        "summary": "Deliverable matches the requirement.",
    }
    if record is not None:
        payload["completion_evidence"] = record
    return json.dumps(payload)


def _valid_record(refs: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "requirement": "out.txt contains the decoded value per the task prompt",
            "observed": "read_output showed value=7 and out.txt contains 7",
            "inspection_refs": refs,
            "falsification_check": "a differing independently derived value would have contradicted the file",
        }
    ]


def _run_verify(
    responses: list[str],
    *,
    packet_overrides: dict[str, Any] | None = None,
    inspector: Any = _grounded_inspector,
) -> tuple[str, list[list[dict[str, str]]]]:
    calls: list[list[dict[str, str]]] = []

    def verifier_model(messages, *, max_output_tokens=8000):
        calls.append(list(messages))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    hooks = ModelHooks(
        architect_model=lambda m, *, max_output_tokens=8000: "{}",
        solver_model=lambda m, *, max_output_tokens=8000: "{}",
        verifier_model=verifier_model,
    )
    packet: dict[str, Any] = {
        "reason": "solver_submit", "task_prompt": "Write out.txt", "artifacts_present": ["out.txt"],
    }
    if packet_overrides:
        packet.update(packet_overrides)
    raw = hooks.verify_with_inspector(
        packet,
        _compiled(),
        ledger=_ledger_stub(),
        inspector=inspector,
    )
    return raw, calls


# ---------------------------------------------------------------------------
# Runtime gate behavior
# ---------------------------------------------------------------------------

def test_completed_without_record_gets_one_protocol_retry_then_accepts_valid_record() -> None:
    raw, calls = _run_verify([
        _INSPECT_REQUEST,
        _completed(None),
        _completed(_valid_record([_inspection_id("probe-1")])),
    ])
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "completed"
    assert len(parsed.completion_evidence) == 1
    assert parsed.completion_evidence[0].inspection_refs == (_inspection_id("probe-1"),)
    assert len(calls) == 3
    retry_instruction = calls[2][-1]["content"]
    assert "completion_evidence" in retry_instruction
    assert "missing or empty" in retry_instruction


def test_completed_with_unresolvable_refs_is_refused_as_protocol_event() -> None:
    raw, calls = _run_verify([
        _INSPECT_REQUEST,
        _completed(_valid_record(["ghost.txt"])),
        _completed(_valid_record(["ghost.txt"])),
    ])
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "uncertain_missing_evidence"
    assert parsed.findings
    assert parsed.findings[0].finding_id == "vf-completion-evidence-record"
    assert parsed.findings[0].applies_to == ("completion_evidence",)
    assert "do not match any inspection" in parsed.summary
    assert len(calls) == 3


def test_completed_with_valid_record_is_accepted_without_retry() -> None:
    raw, calls = _run_verify([
        _INSPECT_REQUEST,
        _completed(_valid_record([_inspection_id("probe-1")])),
    ])
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "completed"
    assert len(calls) == 2


def test_record_with_empty_falsification_field_is_rejected() -> None:
    record = _valid_record([_inspection_id("probe-1")])
    record[0]["falsification_check"] = ""
    raw, _calls = _run_verify([
        _INSPECT_REQUEST,
        _completed(record),
        _completed(record),
    ])
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "uncertain_missing_evidence"
    assert "empty requirement/observed/falsification_check" in parsed.summary


# ---------------------------------------------------------------------------
# Bug fix: refs from a FAILED inspection must not satisfy the gate
# ---------------------------------------------------------------------------

_FAILED_READ_INSPECT_REQUEST = json.dumps({
    "kind": "inspect",
    "summary": "Read the missing file.",
    "requests": [
        {"request_id": "probe-1", "kind": "read_file", "path": "missing.txt"},
    ],
})


def _failing_inspector(requests):
    return [
        {
            "request_id": req.request_id,
            "inspection_id": _inspection_id(req.request_id),
            "kind": req.kind,
            "path": req.path,
            "eligible_for_proof": False,
            "evidence_ceiling": ROUTE_EVIDENCE_CEILINGS[req.kind],
            "error": f"file not found: {req.path}",
        }
        for req in requests
    ]


def test_completed_citing_only_a_failed_inspection_ref_is_refused() -> None:
    raw, calls = _run_verify(
        [
            _FAILED_READ_INSPECT_REQUEST,
            _completed(_valid_record([_inspection_id("probe-1")])),
            _completed(_valid_record([_inspection_id("probe-1")])),
        ],
        inspector=_failing_inspector,
    )
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "uncertain_missing_evidence"
    assert parsed.findings
    assert parsed.findings[0].finding_id == "vf-completion-evidence-record"
    assert "do not match any inspection" in parsed.summary
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Bug fix: a malformed completion_evidence record retries as a record
# problem, never crashes into the generic "not valid protocol JSON" path
# ---------------------------------------------------------------------------

def _completed_with_malformed_record() -> str:
    return json.dumps({
        "verdict": "completed",
        "confidence": "high",
        "summary": "Deliverable matches the requirement.",
        "completion_evidence": ["not-an-object", "also-not-an-object"],
    })


def test_malformed_completion_evidence_shape_retries_as_record_problem_not_json_crash() -> None:
    raw, calls = _run_verify([
        _INSPECT_REQUEST,
        _completed_with_malformed_record(),
        _completed(_valid_record([_inspection_id("probe-1")])),
    ])
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "completed"
    assert len(calls) == 3
    retry_instruction = calls[2][-1]["content"]
    # The SPECIFIC shape problem must be surfaced (proves correct routing)...
    assert "completion_evidence" in retry_instruction
    assert "must be an object" in retry_instruction
    # ...never the generic malformed-JSON instruction, which would misdirect
    # the model toward resending a bare verdict/confidence/summary.
    assert "not valid protocol json" not in retry_instruction.lower()


def test_malformed_completion_evidence_shape_refuses_when_retry_budget_exhausted() -> None:
    raw, calls = _run_verify([
        _INSPECT_REQUEST,
        _completed_with_malformed_record(),
        _completed_with_malformed_record(),
    ])
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "uncertain_missing_evidence"
    assert parsed.findings
    assert parsed.findings[0].finding_id == "vf-completion-evidence-record"
    assert "must be an object" in parsed.summary
    assert len(calls) == 3


def test_malformed_completion_evidence_raises_shape_specific_error() -> None:
    with pytest.raises(CompletionEvidenceShapeError):
        parse_model_verifier_result(json.dumps({
            "verdict": "completed",
            "confidence": "high",
            "summary": "ok",
            "completion_evidence": ["not-an-object"],
        }))
    # Still a ValueError: existing `except ValueError` / `pytest.raises
    # (ValueError)` call sites keep working unchanged.
    assert issubclass(CompletionEvidenceShapeError, ValueError)


# ---------------------------------------------------------------------------
# Independence-kind requirement (Phase 1.5): closes the gcode/video
# false-clean gap left by the content-blind structural gate alone. See
# FABLE5_BATCH_AUDIT_20260709T101515Z.md secs 4 and 6.
# ---------------------------------------------------------------------------

_READ_FILE_INSPECT_REQUEST = json.dumps({
    "kind": "inspect",
    "summary": "Read the output file directly.",
    "requests": [
        {"request_id": "probe-1", "kind": "read_file", "path": "out.txt"},
    ],
})

_OVERLAY_INSPECT_REQUEST = json.dumps({
    "kind": "inspect",
    "summary": "Read the file, then independently recompute the value in the overlay.",
    "requests": [
        {"request_id": "probe-1", "kind": "read_file", "path": "out.txt"},
        {"request_id": "probe-2", "kind": "overlay_run_command", "command": "python3 recompute.py"},
    ],
})

_RE_DERIVABLE_PACKET = {"re_derivable_claims": ["the decoded value in out.txt is machine-re-derivable"]}


def test_completed_with_only_read_file_refs_is_refused_when_independence_required() -> None:
    raw, calls = _run_verify(
        [
            _READ_FILE_INSPECT_REQUEST,
            _completed(_valid_record([_inspection_id("probe-1")])),
            _completed(_valid_record([_inspection_id("probe-1")])),
        ],
        packet_overrides=_RE_DERIVABLE_PACKET,
    )
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "uncertain_missing_evidence"
    assert parsed.findings
    assert parsed.findings[0].finding_id == "vf-completion-evidence-independence"
    assert "independent-derivation" in parsed.summary
    assert len(calls) == 3


def test_completed_with_overlay_run_command_ref_is_accepted_when_independence_required() -> None:
    raw, calls = _run_verify(
        [
            _OVERLAY_INSPECT_REQUEST,
            _completed(_valid_record([_inspection_id("probe-1"), _inspection_id("probe-2")])),
        ],
        packet_overrides=_RE_DERIVABLE_PACKET,
    )
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "completed"
    assert len(calls) == 2


def test_completed_with_only_read_file_refs_is_accepted_when_independence_not_flagged() -> None:
    # Same record/refs as the refused case above, but re_derivable_claims is
    # unset: unchanged legacy behavior, no independence requirement applied.
    raw, calls = _run_verify([
        _READ_FILE_INSPECT_REQUEST,
        _completed(_valid_record([_inspection_id("probe-1")])),
    ])
    parsed = parse_model_verifier_result(raw)
    assert parsed.verdict == "completed"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Helper-level checks (content-blind semantics)
# ---------------------------------------------------------------------------

def test_completion_record_problem_is_content_blind() -> None:
    entry = CompletionEvidenceEntry(
        requirement="anything",
        observed="anything",
        falsification_check="anything",
        inspection_refs=("probe-1",),
    )
    result = type("_R", (), {"completion_evidence": (entry,)})()
    assert _completion_record_problem(result, {"probe-1"}) == ""
    assert "do not match" in _completion_record_problem(result, {"other"})
    empty = type("_R", (), {"completion_evidence": ()})()
    assert "missing or empty" in _completion_record_problem(empty, {"probe-1"})


def test_refs_include_only_registered_inspection_ids() -> None:
    request = VerifierInspectionRequest(request_id="r-1", kind="read_file", path="out.txt")
    refs = _refs_from_inspections((request,), [{
        "request_id": "r-1",
        "inspection_id": "inspection:test:r-1",
        "handle": "2:a-1:stdout",
        "eligible_for_proof": True,
    }])
    assert refs == {"inspection:test:r-1"}


def test_refs_from_errored_inspection_result_are_excluded() -> None:
    request = VerifierInspectionRequest(request_id="r-1", kind="read_file", path="missing.txt")
    refs = _refs_from_inspections(
        (request,),
        [{"request_id": "r-1", "path": "missing.txt", "error": "file not found: missing.txt"}],
    )
    assert refs == set()


def test_refs_from_negative_but_non_errored_probe_are_included() -> None:
    # A probe that ran successfully and observed a negative result (port
    # closed) is NOT an inspection error -- judging whether "closed" supports
    # or contradicts a claim is content, and content stays the model's job.
    request = VerifierInspectionRequest(request_id="r-1", kind="probe_port", target="127.0.0.1:9")
    refs = _refs_from_inspections(
        (request,),
        [{
            "request_id": "r-1",
            "inspection_id": "inspection:test:r-1",
            "kind": "probe_port",
            "host": "127.0.0.1",
            "port": 9,
            "state": "closed",
            "eligible_for_proof": True,
        }],
    )
    assert refs == {"inspection:test:r-1"}


def test_independent_derivation_refs_excludes_read_file_includes_overlay() -> None:
    read_request = VerifierInspectionRequest(request_id="r-1", kind="read_file", path="out.txt")
    overlay_request = VerifierInspectionRequest(request_id="r-2", kind="overlay_run_command", command="echo hi")
    results = [
        {"request_id": "r-1", "inspection_id": "inspection:test:r-1", "kind": "read_file", "path": "out.txt", "excerpt": "7", "eligible_for_proof": True},
        {"request_id": "r-2", "inspection_id": "inspection:test:r-2", "kind": "overlay_run_command", "exit_code": 0, "success": True, "stdout": "hi", "eligible_for_proof": True},
    ]
    refs = _independent_derivation_refs((read_request, overlay_request), results)
    assert refs == {"inspection:test:r-2"}


def test_independent_derivation_refs_excludes_errored_overlay_command() -> None:
    overlay_request = VerifierInspectionRequest(request_id="r-2", kind="overlay_run_command", command="echo hi")
    results = [{"request_id": "r-2", "kind": "overlay_run_command", "error": "no overlay available"}]
    refs = _independent_derivation_refs((overlay_request,), results)
    assert refs == set()


def test_completion_independence_problem_is_content_blind() -> None:
    entry = CompletionEvidenceEntry(
        requirement="anything", observed="anything", falsification_check="anything",
        inspection_refs=("r-1", "r-2"),
    )
    result = type("_R", (), {"completion_evidence": (entry,)})()
    assert _completion_independence_problem(result, {"r-2"}) == ""
    problem = _completion_independence_problem(result, {"other"})
    assert "independent-derivation" in problem


# ---------------------------------------------------------------------------
# Parse-layer normalization
# ---------------------------------------------------------------------------

def test_parse_normalizes_completion_evidence_entries() -> None:
    parsed = parse_model_verifier_result(json.dumps({
        "verdict": "completed",
        "confidence": "high",
        "summary": "ok",
        "completion_evidence": [
            {
                "requirement": "req",
                "observed": "obs",
                "inspection_refs": "probe-1",
                "falsification_check": "would differ",
            }
        ],
    }))
    assert parsed.completion_evidence[0].inspection_refs == ("probe-1",)
    assert parsed.as_dict()["completion_evidence"][0]["requirement"] == "req"


def test_parse_rejects_malformed_completion_evidence() -> None:
    with pytest.raises(ValueError):
        parse_model_verifier_result(json.dumps({
            "verdict": "completed",
            "confidence": "high",
            "summary": "ok",
            "completion_evidence": ["not-an-object"],
        }))
    with pytest.raises(ValueError):
        parse_model_verifier_result(json.dumps({
            "verdict": "completed",
            "confidence": "high",
            "summary": "ok",
            "completion_evidence": [{"requirement": "r", "observed": "o", "inspection_refs": 7}],
        }))


# ---------------------------------------------------------------------------
# Contract + doctrine advertisement
# ---------------------------------------------------------------------------

def test_contract_advertises_completion_evidence_protocol() -> None:
    assert VERIFIER_RUNTIME_CONTRACT["required_fields"]["completed"] == ["completion_evidence"]
    shape = VERIFIER_RUNTIME_CONTRACT["completion_evidence_shape"]
    assert set(shape) == {"requirement", "observed", "inspection_refs", "falsification_check"}
    rules = " ".join(VERIFIER_RUNTIME_CONTRACT["rules"])
    assert "machine-re-derivable" in rules
    assert "overlay_run_command" in rules


def test_contract_states_independence_kind_requirement_plainly() -> None:
    rules = " ".join(VERIFIER_RUNTIME_CONTRACT["rules"])
    assert "Runtime-enforced, not prompt-only" in rules
    assert "re_derivable_claims" in rules
    assert "independent-derivation inspection kind" in rules
    for kind in (
        "overlay_run_command", "rerun_check", "probe_port", "probe_http",
        "probe_process", "perceive_artifact",
    ):
        assert kind in rules
    assert "will be refused" in rules


def test_contract_respects_structured_record_delimiters() -> None:
    rules = " ".join(VERIFIER_RUNTIME_CONTRACT["rules"])
    assert "declared record grammar or field delimiters" in rules


def test_architect_doctrine_requires_method_independent_minimum_evidence() -> None:
    assert "method-independent" in WORKBENCH_ARCHITECT_SYSTEM_PROMPT
    assert "machine-re-derivable" in WORKBENCH_ARCHITECT_SYSTEM_PROMPT
    assert "around the solver's reported values" in WORKBENCH_ARCHITECT_SYSTEM_PROMPT
    assert "model_context_window_tokens" in WORKBENCH_ARCHITECT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Context-window de-starvation
# ---------------------------------------------------------------------------

def _raw_config(**context_policy: Any) -> str:
    base = {
        "schema_version": "harness_config.v1",
        "task_understanding": "Write one output file.",
        "success_definition": "out.txt exists and matches the prompt.",
        "solver_system_prompt": {"role": "Careful file task solver"},
        "verifier_system_prompt": {
            "role": "Task-specific evidence verifier",
            "success_criteria": ["out.txt matches the requested result"],
            "required_evidence": ["artifact content checked against the task"],
        },
        "evidence_requirements": ["out.txt content matches the requested result"],
        "minimum_completion_evidence": ["independent content evidence"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "run_command"]},
        "context_policy": {"mode": "default_bounded", **context_policy},
    }
    return json.dumps(base)


def test_context_window_default_is_modern_not_starved() -> None:
    config = parse_harness_config_ir(_raw_config())
    assert config.context_policy.model_context_window_tokens == 50_000
    runtime_ir = harness_config_to_runtime_ir(config, _make_envmap())
    assert runtime_ir.context_policy.model_context_window_tokens == 50_000


def test_architect_can_set_context_window_and_it_reaches_runtime() -> None:
    config = parse_harness_config_ir(_raw_config(model_context_window_tokens=120_000))
    assert config.context_policy.model_context_window_tokens == 120_000
    runtime_ir = harness_config_to_runtime_ir(config, _make_envmap())
    assert runtime_ir.context_policy.model_context_window_tokens == 120_000


def test_context_window_bounds_are_fail_closed() -> None:
    from aether_next.model_hooks import ModelOutputError

    with pytest.raises(ModelOutputError):
        parse_harness_config_ir(_raw_config(model_context_window_tokens=500))
    with pytest.raises(ModelOutputError):
        parse_harness_config_ir(_raw_config(model_context_window_tokens="lots"))


def test_architect_workbench_compiles_re_derivable_claims_and_reaches_runtime_ir() -> None:
    raw = {
        "schema_version": "harness_config.v1",
        "task_understanding": "Write one output file.",
        "success_definition": "out.txt exists and matches the prompt.",
        "solver_system_prompt": {"role": "Careful file task solver"},
        "verifier_system_prompt": {
            "role": "Task-specific evidence verifier",
            "success_criteria": ["out.txt matches the requested result"],
            "required_evidence": ["artifact content checked against the task"],
        },
        "evidence_requirements": ["out.txt content matches the requested result"],
        "minimum_completion_evidence": ["independent content evidence"],
        "re_derivable_claims": ["the hash of out.txt matches the expected"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "run_command"]},
        "context_policy": {"mode": "default_bounded"},
    }
    config = parse_harness_config_ir(json.dumps(raw))
    assert config.re_derivable_claims == ("the hash of out.txt matches the expected",)
    runtime_ir = harness_config_to_runtime_ir(config, _make_envmap())
    assert runtime_ir.re_derivable_claims == ("the hash of out.txt matches the expected",)
