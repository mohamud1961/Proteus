from __future__ import annotations

import json
from pathlib import Path

import pytest

from run_trace_verifier_replay_ab import run, DEFAULT_CASES


def _require_trace_replay_fixtures() -> None:
    missing = [trace for _task, trace in DEFAULT_CASES if not Path(trace).exists()]
    if missing:
        pytest.skip("trace verifier replay fixtures not packaged in code-only archive: " + ", ".join(missing[:3]))


def test_trace_verifier_replay_ab_fake_writes_variant_artifacts(tmp_path) -> None:
    _require_trace_replay_fixtures()
    summary = run(mode="fake", out_dir=tmp_path)

    assert summary["counts"]["cases"] == 3
    assert summary["counts"]["ok"] == 3
    assert summary["counts"]["architect_prompt_improved"] == 0
    assert summary["counts"]["prompt_only_invention_blocked"] == 3
    for row in summary["rows"]:
        task = row["task"]
        for variant in ("generic", "architect_prompt"):
            variant_dir = tmp_path / task / variant
            assert (variant_dir / "verifier_packet.json").exists()
            assert (variant_dir / "raw_output.json").exists()
            assert (variant_dir / "verifier_prompt.txt").exists()
            assert (variant_dir / "parsed_result.json").exists()
            assert (variant_dir / "active_findings_after.json").exists()
            assert (variant_dir / "judgement.json").exists()
        architect_packet = json.loads((tmp_path / task / "architect_prompt" / "verifier_packet.json").read_text())
        architect_prompt = (tmp_path / task / "architect_prompt" / "verifier_prompt.txt").read_text()
        assert architect_prompt.strip()
        assert "architect_verifier_prompt" not in architect_packet
        assert "verifier_prompt" not in architect_packet
