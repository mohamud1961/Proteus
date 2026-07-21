from __future__ import annotations

import json
from pathlib import Path

from evals.checks import BUILTINS
from evals.framework import run_manifest


BUILD_ROOT = Path(__file__).resolve().parents[1]


def _write_manifest(root: Path, cases: list[dict], scorecard_ids: list[str]) -> Path:
    evals = root / "evals"
    evals.mkdir(parents=True)
    path = evals / "manifest.json"
    path.write_text(json.dumps({
        "schema": "aether.harness_eval_manifest.v1",
        "scorecard_ids": scorecard_ids,
        "cases": cases,
    }), encoding="utf-8")
    return path


def test_framework_finalizes_a_passing_builtin_run(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "id": "coverage",
        "layer": "meta",
        "kind": "builtin",
        "builtin": "scorecard_eval_coverage",
        "required": True,
        "covers": ["T1"],
    }], ["T1"])
    result = run_manifest(manifest, output_dir=tmp_path / "out")
    assert result.passed is True
    assert result.required_failures == ()
    assert Path(result.final_marker["path"]).is_file()
    assert (tmp_path / "out" / "summary.json").is_file()
    assert (tmp_path / "out" / "cases" / "coverage" / "result.json").is_file()


def test_framework_fails_closed_on_missing_pytest_target(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "id": "missing",
        "layer": "component",
        "kind": "pytest",
        "required": True,
        "covers": ["T1"],
        "targets": ["tests/does_not_exist.py"],
    }], ["T1"])
    result = run_manifest(manifest, output_dir=tmp_path / "out")
    assert result.passed is False
    assert result.required_failures == ("missing",)
    case = result.cases[0]
    assert case.status == "error"
    assert "missing target" in case.findings[0]
    assert Path(result.final_marker["path"]).is_file()


def test_plan_cases_never_run_models_inside_generic_framework(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, [{
        "id": "model",
        "layer": "model_system",
        "kind": "plan",
        "gate": "model",
        "required": False,
        "covers": ["T1"],
        "board": "smoke",
        "samples": 3,
    }], ["T1"])
    planned = run_manifest(manifest, output_dir=tmp_path / "planned")
    assert planned.cases[0].status == "planned"
    enabled = run_manifest(manifest, output_dir=tmp_path / "enabled", allow_model=True)
    assert enabled.cases[0].status == "ready_for_external_runner"
    assert enabled.cases[0].command == ()


def test_all_manifest_builtins_are_registered() -> None:
    payload = json.loads((BUILD_ROOT / "evals" / "manifest.v1.json").read_text(encoding="utf-8"))
    names = {case["builtin"] for case in payload["cases"] if case["kind"] == "builtin"}
    assert names <= set(BUILTINS)


def test_context_growth_builtin_exercises_production_context() -> None:
    payload = BUILTINS["context_growth_probe"](BUILD_ROOT, {})
    assert payload["metrics"]["packet_bytes_at_640_receipts"] > 0
    assert payload["metrics"]["large_packet_command_results"] <= 8
