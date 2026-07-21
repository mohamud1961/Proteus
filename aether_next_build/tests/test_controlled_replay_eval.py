from __future__ import annotations

import pytest
from run_controlled_replay_eval import CASES, build_case_record, run, summarize


def _require_replay_fixtures() -> None:
    missing = [str(case.trace_path) for case in CASES if not case.trace_path.exists()]
    if missing:
        pytest.skip("controlled replay trace fixtures not packaged in code-only archive: " + ", ".join(missing[:3]))


def test_controlled_replay_report_uses_real_trace_evidence() -> None:
    _require_replay_fixtures()
    records = [build_case_record(case) for case in CASES]
    summary = summarize(records)

    assert summary["case_count"] == 3
    assert summary["model_hint_present"] is False
    assert summary["axis_status_counts"]["query_memory weak/absent vs enriched memory/tool guidance"][
        "evidence_limited"
    ] == 3
    assert summary["metric_totals"]["repair_hints_count"] == 0
    assert summary["metric_totals"]["files_already_read_count"] >= 1

    filter_js = records[0]
    statuses = {axis["axis"]: axis["status"] for axis in filter_js["axes"]}
    assert statuses["old_context vs enriched_deterministic_context"] == "pass"
    assert statuses["compression/simple vs current enriched context"] == "pass"
    assert filter_js["metrics"]["model_hint_present"] is False
    assert filter_js["metrics"]["enriched_context_key_count"] >= filter_js["metrics"]["old_context_key_count"]
    assert filter_js["metrics"]["pending_checks_count"] == 0


def test_controlled_replay_run_writes_expected_artifacts(tmp_path) -> None:
    _require_replay_fixtures()
    result = run(tmp_path)

    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "CONTROLLED_REPLAY_REPORT.md").exists()
    assert result["summary"]["case_count"] == 3
