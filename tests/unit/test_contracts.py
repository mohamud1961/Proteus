from pathlib import Path

import pytest

from proteus.architect import Architect
from proteus.models import Deliverable, SolverAction, TaskSpec
from proteus.providers import OpenAIProvider


def test_architect_compiles_bounded_workbench(tmp_path: Path) -> None:
    task = TaskSpec("t", "build an artifact", (Deliverable("out.txt", "ok"),))
    workbench = Architect().synthesize(task, tmp_path)
    assert workbench.task_fingerprint == task.fingerprint()
    assert "write_file" in workbench.allowed_actions
    assert workbench.workspace_root == str(tmp_path)


def test_live_provider_is_explicitly_unavailable_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider()
    assert provider.availability()["available"] is False
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        provider.require_credentials()


def test_action_is_serializable() -> None:
    assert SolverAction("a", "run_command", command=("python", "-V")).as_dict()["action_id"] == "a"
