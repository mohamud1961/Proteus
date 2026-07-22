from __future__ import annotations

from run_automatic_memory_diagnostic_eval import run


def test_automatic_memory_diagnostic_eval_scores_policy_modes(tmp_path) -> None:
    summary = run(tmp_path)

    assert summary["counts"] == {"rows": 12, "passed": 12, "failed": 0}
    rows = {(row["case"], row["mode"]): row for row in summary["rows"]}
    assert rows[("repeat_read", "off")]["automatic_memory_count"] == 0
    assert rows[("repeat_read", "advisory")]["automatic_memory_count"] == 1
    assert rows[("repeat_read", "soft_block_exact_repeat")]["advisory_count"] == 1
    assert rows[("repeat_command", "require_justification")]["advisory_count"] == 1
    assert rows[("justified_repeat_read", "soft_block_exact_repeat")]["advisory_count"] == 0
