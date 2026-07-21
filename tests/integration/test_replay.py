from pathlib import Path

from proteus.cli import run_replay


def test_replay_executes_and_verifies(tmp_path: Path) -> None:
    result = run_replay(tmp_path)
    assert result.mode == "replay"
    assert result.verification.passed
    assert [item.kind for item in result.actions] == ["write_file", "read_file", "run_command"]
    assert Path(result.evidence_path).is_file()
    assert "verification" in Path(result.evidence_path).read_text(encoding="utf-8")
