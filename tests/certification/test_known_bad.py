from pathlib import Path

from proteus.cli import demo_task, run_replay
from proteus.verifier import IndependentVerifier


def test_independent_verifier_rejects_tampered_artifact(tmp_path: Path) -> None:
    result = run_replay(tmp_path)
    artifact = tmp_path / demo_task().deliverables[0].path
    artifact.write_text("print('tampered')\n", encoding="utf-8")
    rejected = IndependentVerifier().verify(demo_task(), tmp_path)
    assert result.verification.passed
    assert not rejected.passed
