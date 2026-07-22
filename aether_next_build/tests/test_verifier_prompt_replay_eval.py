from __future__ import annotations

import json

from run_verifier_prompt_replay_eval import run


def test_verifier_prompt_replay_eval_writes_packet_raw_parsed_and_judgement(tmp_path) -> None:
    summary = run(tmp_path)

    assert summary["state_only_packet_actionable"] is True
    rows = {row["variant"]: row for row in summary["rows"]}
    assert rows["generic"]["verdict"] == "needs_repair"
    assert rows["architect_prompt"]["verdict"] == "needs_repair"
    assert rows["architect_prompt"]["evidence_bound"] is True
    assert rows["architect_prompt"]["specific_repair"] is True

    for variant in ("generic", "architect_prompt"):
        variant_dir = tmp_path / variant
        assert (variant_dir / "verifier_packet.json").exists()
        assert (variant_dir / "raw_output.json").exists()
        assert (variant_dir / "parsed_result.json").exists()
        assert (variant_dir / "active_findings_after.json").exists()
        assert (variant_dir / "judgement.json").exists()

    architect_packet = json.loads((tmp_path / "architect_prompt" / "verifier_packet.json").read_text())
    assert "architect_verifier_prompt" not in json.dumps(architect_packet, sort_keys=True)
    assert "automatic_memory_findings" not in architect_packet
    assert "solver_authored_evidence" not in architect_packet
