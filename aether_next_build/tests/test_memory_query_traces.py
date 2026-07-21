from __future__ import annotations

import json
from pathlib import Path

import pytest

from aether_next.ledger import ExecutionLedger, Receipt


TRACE_ROOT = Path(__file__).resolve().parents[1] / "phase2_traces"


def _require_trace(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"trace fixture not packaged in code-only archive: {path}")


def _command_from_summary(summary: str) -> str:
    marker = ": "
    if marker in summary:
        return summary.split(marker, 1)[1]
    return summary


def _ledger_from_trace(path: Path) -> ExecutionLedger:
    _require_trace(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    ledger = ExecutionLedger()
    for step in payload["trace"]["steps"]:
        step_no = int(step["step"])
        for obs in step["observations"]:
            receipt_payload = {
                key: obs[key]
                for key in ("path", "stdout_tail", "stderr_tail", "exit_code")
                if key in obs and obs[key] not in (None, "")
            }
            if obs["kind"] == "run_command":
                receipt_payload["command"] = _command_from_summary(obs["summary"])
            ledger.record(Receipt(
                receipt_id=obs["receipt_id"],
                step=step_no,
                kind=obs["kind"],
                success=bool(obs["success"]),
                summary=obs["summary"],
                failure_class=obs.get("failure_class", ""),
                payload=receipt_payload,
            ))
    return ledger


def test_query_memory_answers_prior_file_read_from_old_sparql_trace() -> None:
    ledger = _ledger_from_trace(TRACE_ROOT / "codex" / "sparql-university.trace.json")

    hits = ledger.query_memory(
        "university graph ttl",
        filters={"kind": ("read_file",), "path": "university_graph.ttl"},
    )
    guard = ledger.repeat_guard(kind="read_file", target="university_graph.ttl")

    assert hits
    assert hits[0]["kind"] == "read_file"
    assert hits[0]["path"] == "university_graph.ttl"
    assert guard["repeat_count"] == 4
    assert guard["likely_wasteful"] is True


def test_query_memory_recovers_prior_command_output_content_from_old_trace() -> None:
    ledger = _ledger_from_trace(TRACE_ROOT / "codex" / "sparql-university.trace.json")

    hits = ledger.query_memory(
        "locatedInCountry Sorbonne",
        filters={"kind": ("run_command",)},
    )

    assert hits
    assert hits[0]["kind"] == "run_command"
    assert "sed -n" in hits[0]["command"]
    assert "locatedInCountry" in hits[0]["stdout_tail"]
    assert "Sorbonne" in hits[0]["stdout_tail"]


def test_query_memory_can_flag_repeated_command_from_old_trace() -> None:
    ledger = _ledger_from_trace(TRACE_ROOT / "codex" / "sparql-university.trace.json")
    command = "sed -n '1,260p' /app/university_graph.ttl"

    guard = ledger.repeat_guard(kind="run_command", target=command)
    hits = ledger.query_memory("sed 1 260 university graph", filters={"kind": ("run_command",)})

    assert guard["repeat_count"] >= 3
    assert guard["likely_wasteful"] is True
    assert hits
    assert hits[0]["command"] == command


def test_query_memory_recovers_failed_smoke_context_from_old_filter_trace() -> None:
    ledger = _ledger_from_trace(TRACE_ROOT / "codex" / "filter-js-from-html.trace.json")

    hits = ledger.query_memory("javascript onclick table", filters={"kind": ("run_command",)})

    assert hits
    assert "javascript" in hits[0]["command"].lower() or "javascript" in hits[0].get("stdout_tail", "").lower()
    assert "table" in hits[0]["command"].lower() or "table" in hits[0].get("stdout_tail", "").lower()
