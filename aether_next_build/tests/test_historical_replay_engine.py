from __future__ import annotations

import json
from pathlib import Path

import pytest

from replay_engine.builder import (
    ReplayBuildError,
    build_expectations,
    build_historical_manifest,
    manifest_sha256,
    validate_manifest,
)
from replay_engine.promotion import evaluate_promotion, first_divergence


ROOT = Path(__file__).resolve().parents[1]


def test_historical_manifest_has_required_23_case_progression() -> None:
    manifest = build_historical_manifest(ROOT)
    assert manifest["counts"] == {
        "architect_only": 8,
        "solver_pre_turn_checkpoints": 6,
        "frozen_verifier_packets": 9,
        "total": 23,
    }
    assert manifest["manifest_sha256"] == manifest_sha256(manifest)
    assert {case["fidelity"] for case in manifest["cases"]} >= {
        "exact_input_reconstructed", "exact_pre_turn_context", "reconstructed_state_only",
    }


def test_manifest_rebuild_is_deterministic() -> None:
    first = build_historical_manifest(ROOT)
    second = build_historical_manifest(ROOT)
    assert first == second


def test_role_input_has_evaluator_boundary() -> None:
    manifest = build_historical_manifest(ROOT)
    for case in manifest["cases"]:
        encoded = json.dumps(case["role_input"], sort_keys=True)
        assert "official_reward" not in encoded
        assert "historical_model_response" not in encoded
        assert "future_trace" not in encoded


def test_validator_rejects_evaluator_key_leak() -> None:
    manifest = build_historical_manifest(ROOT)
    bad = json.loads(json.dumps(manifest))
    bad["cases"][0]["role_input"]["prior_verdict"] = "completed"
    with pytest.raises(ReplayBuildError, match="evaluator-only"):
        validate_manifest(bad)


def test_validator_rejects_archive_hash_mismatch() -> None:
    manifest = build_historical_manifest(ROOT)
    bad = json.loads(json.dumps(manifest))
    bad["cases"][0]["provenance"]["sha256"] = "0" * 64
    # Provenance is an input-integrity assertion; the builder must not accept
    # a case whose declared source hash no longer matches the source file.
    source = Path(bad["cases"][0]["provenance"]["source"])
    assert source.exists()
    with pytest.raises(ReplayBuildError, match="hash"):
        validate_manifest(bad)


def test_validator_rejects_duplicate_case_ids() -> None:
    manifest = build_historical_manifest(ROOT)
    bad = json.loads(json.dumps(manifest))
    bad["cases"][1]["case_id"] = bad["cases"][0]["case_id"]
    with pytest.raises(ReplayBuildError, match="duplicate"):
        validate_manifest(bad)


def test_solver_role_input_excludes_future_trace() -> None:
    manifest = build_historical_manifest(ROOT)
    for case in manifest["cases"]:
        if case["replay_type"] == "solver_pre_turn_checkpoint":
            assert "future_trace" not in case["role_input"]
            assert "future_trace" in case["evaluator_only"]


def test_frozen_verifier_cases_are_exact_and_packet_shaped() -> None:
    manifest = build_historical_manifest(ROOT)
    verifier_cases = [case for case in manifest["cases"] if case["replay_type"] == "frozen_verifier_packet"]
    assert len(verifier_cases) == 9
    assert all(case["fidelity"] == "reconstructed_state_only" for case in verifier_cases)
    assert all(isinstance(case["role_input"], dict) for case in verifier_cases)
    forbidden = {"architect_verifier_prompt", "solver_system_prompt", "solver_reported_blockers", "config_realization", "official_grader_authority"}
    for case in verifier_cases:
        encoded = json.dumps(case["role_input"], sort_keys=True)
        assert not any(key in encoded for key in forbidden)


def test_validator_rejects_missing_provenance_source() -> None:
    manifest = build_historical_manifest(ROOT)
    bad = json.loads(json.dumps(manifest))
    bad["cases"][0]["provenance"]["source"] = "/tmp/does-not-exist-replay-source"
    with pytest.raises(ReplayBuildError, match="provenance source hash mismatch"):
        validate_manifest(bad)


def test_every_case_has_content_hash_provenance() -> None:
    manifest = build_historical_manifest(ROOT)
    assert all(len(case["provenance"]["sha256"]) == 64 for case in manifest["cases"])


def test_manifest_hash_survives_json_roundtrip() -> None:
    manifest = build_historical_manifest(ROOT)
    roundtrip = json.loads(json.dumps(manifest, sort_keys=True))
    assert manifest_sha256(roundtrip) == manifest["manifest_sha256"]


def test_expectations_are_evaluator_only_and_cover_all_cases() -> None:
    manifest = build_historical_manifest(ROOT)
    expectations = build_expectations(manifest)
    assert set(expectations["cases"]) == {case["case_id"] for case in manifest["cases"]}
    assert all("expected_replay_outcome" not in case["role_input"] for case in manifest["cases"])


def test_promotion_is_fail_closed_until_every_stage_has_evidence() -> None:
    manifest = build_historical_manifest(ROOT)
    decision = evaluate_promotion(manifest)
    assert decision.status == "NOT READY FOR UNRESTRICTED FULL RUNS"
    assert {gate.name for gate in decision.gates} == {
        "architect_only_8",
        "compiler_workbench_board",
        "verifier_frozen_9",
        "solver_checkpoints_6",
        "first_divergence_replay",
        "short_sentinels",
    }
    assert all(gate.status == "blocked" for gate in decision.gates)


def test_promotion_requires_evidence_paths_even_for_pass_status(tmp_path: Path) -> None:
    manifest = build_historical_manifest(ROOT)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    evidence = {
        name: {"status": "passed", "evidence": str(evidence_root / f"{name}.json")}
        for name in (
            "architect_only_8", "compiler_workbench_board", "verifier_frozen_9",
            "solver_checkpoints_6", "first_divergence_replay", "short_sentinels",
        )
    }
    for item in evidence.values():
        Path(item["evidence"]).write_text("{\"verified\":true}\n", encoding="utf-8")
    decision = evaluate_promotion(manifest, gate_evidence=evidence)
    assert decision.ready
    assert decision.status == "READY FOR UNRESTRICTED FULL RUNS"

    evidence["short_sentinels"] = {"status": "passed"}
    assert not evaluate_promotion(manifest, gate_evidence=evidence).ready


def test_promotion_rejects_tampered_manifest_hash(tmp_path: Path) -> None:
    manifest = build_historical_manifest(ROOT)
    manifest["cases"][0]["role_input"]["task"] = "tampered"
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    evidence = {}
    for name in (
        "architect_only_8", "compiler_workbench_board", "verifier_frozen_9",
        "solver_checkpoints_6", "first_divergence_replay", "short_sentinels",
    ):
        path = evidence_root / f"{name}.json"
        path.write_text("verified\n", encoding="utf-8")
        evidence[name] = {"status": "passed", "evidence": str(path)}
    decision = evaluate_promotion(manifest, gate_evidence=evidence)
    assert not decision.ready
    assert all("SHA256 mismatch" in gate.detail for gate in decision.gates)


def test_first_divergence_is_deterministic_and_length_aware() -> None:
    assert first_divergence([{"step": 1}, {"step": 2}], [{"step": 1}, {"step": 3}])["index"] == 1
    assert first_divergence([1, 2], [1]) == {"index": 1, "expected": 2, "observed": None, "kind": "length_mismatch"}
    assert first_divergence([1, 2], [1, 2]) is None


def test_promotion_accepts_source_complete_manifest_shape_but_stays_blocked_without_stage_evidence() -> None:
    source = Path("/private/tmp/aether_v61/AETHER_NEXT_SOURCE_COMPLETE_HANDOFF_V6_1_20260711/replay_engine/generated/historical_replay_manifest.json")
    if not source.exists():
        pytest.skip("external V6.1 replay manifest is unavailable")
    manifest = json.loads(source.read_text())
    decision = evaluate_promotion(manifest)
    assert decision.status == "NOT READY FOR UNRESTRICTED FULL RUNS"
    assert {gate.status for gate in decision.gates} == {"blocked"}
