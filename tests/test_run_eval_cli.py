"""Hermetic offline test for the runner run-eval CLI.

Invokes the CLI on the mcp_registry_contract_smoke eval pack, which uses a
deterministic LocalStubModelClient (no API keys, no network) and asserts that
it produces a valid score JSON with passed=True.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner.cli import run_eval

PACK_PATH = (
    Path(__file__).resolve().parents[1]
    / "eval_suite/families/tooling/mcp_registry_contract_smoke/task_pack.json"
)


def test_run_eval_returns_passing_score() -> None:
    """LocalStub agent solves mcp_registry_contract_smoke offline; score == 1.0."""
    assert PACK_PATH.exists(), f"pack not found: {PACK_PATH}"
    result = run_eval(str(PACK_PATH))

    assert isinstance(result, dict)
    assert result["task_id"] == "mcp_registry_contract_smoke_v1"
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert result["verdict"] == "pass"
    assert result["reason_codes"] == []


def test_run_eval_result_is_json_serialisable() -> None:
    """Result dict must round-trip through JSON (required for CLI output)."""
    result = run_eval(str(PACK_PATH))
    serialized = json.dumps(result)
    roundtrip = json.loads(serialized)
    assert roundtrip["passed"] is True
    assert roundtrip["score"] == 1.0


def test_run_eval_with_directory_arg() -> None:
    """CLI also accepts the pack directory path, not just the .json file."""
    result = run_eval(str(PACK_PATH.parent))
    assert result["passed"] is True


def test_run_eval_missing_pack_raises() -> None:
    """FileNotFoundError raised when pack path does not exist."""
    with pytest.raises(FileNotFoundError):
        run_eval("/nonexistent/path/task_pack.json")
