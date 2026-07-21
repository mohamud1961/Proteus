#!/usr/bin/env python3
"""Validate verifier-only experiment artifacts.

This is an offline quality gate for outputs produced by run_verifier_only_eval.py.
It does not call a model, run a solver, start Docker, execute benchmark tasks, or
consult the official grader.  It checks that a verifier-only experiment bundle is
complete, parseable, evidence-bound, actionable, and free of common secret leaks.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_CASES = {
    "semantic_wrong",
    "solver_claim_conflicts_with_raw_state",
    "missing_artifact",
    "schema_mismatch",
    "repeated_no_progress",
    "insufficient_evidence",
}

SECRET_PATTERNS = (
    re.compile(r"AZURE_OPENAI_[A-Z0-9_]*KEY", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
)


@dataclass(frozen=True)
class CaseValidation:
    case: str
    ok: bool
    problems: tuple[str, ...]
    parsed_verdict: str
    active_findings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "ok": self.ok,
            "problems": list(self.problems),
            "parsed_verdict": self.parsed_verdict,
            "active_findings": list(self.active_findings),
        }


def validate_bundle(out_dir: Path, *, require_all_cases: bool = True) -> dict[str, Any]:
    problems: list[str] = []
    case_results: list[CaseValidation] = []
    if not out_dir.exists() or not out_dir.is_dir():
        return _result(out_dir, False, [f"missing output directory: {out_dir}"], [])

    summary = _read_json(out_dir / "summary.json", problems, required=True)
    if not isinstance(summary, dict):
        summary = {}
    report_path = out_dir / "VERIFIER_ONLY_EXPERIMENT_REPORT.md"
    if not report_path.exists():
        problems.append("missing VERIFIER_ONLY_EXPERIMENT_REPORT.md")

    rows = summary.get("rows", [])
    if not isinstance(rows, list):
        problems.append("summary.rows must be a list")
        rows = []
    row_cases = {str(row.get("case", "")) for row in rows if isinstance(row, dict)}
    if require_all_cases and row_cases != EXPECTED_CASES:
        problems.append(f"summary case set mismatch: got {sorted(row_cases)}, expected {sorted(EXPECTED_CASES)}")

    for case in sorted(EXPECTED_CASES if require_all_cases else row_cases):
        case_results.append(_validate_case(out_dir, case))

    secret_hits = _scan_for_secrets(out_dir)
    if secret_hits:
        problems.extend([f"possible secret leak: {hit}" for hit in secret_hits])

    all_ok = not problems and all(item.ok for item in case_results)
    return _result(out_dir, all_ok, problems, case_results)


def _validate_case(out_dir: Path, case: str) -> CaseValidation:
    case_dir = out_dir / case
    problems: list[str] = []
    if not case_dir.exists():
        return CaseValidation(case, False, ("missing case directory",), "", ())
    packet = _read_json(case_dir / "verifier_packet.json", problems, required=True)
    raw_path = case_dir / "raw_output.json"
    parsed = _read_json(case_dir / "parsed_result.json", problems, required=True)
    findings_after = _read_json(case_dir / "active_findings_after.json", problems, required=True)
    judgement = _read_json(case_dir / "judgement.json", problems, required=True)

    if not raw_path.exists():
        problems.append("missing raw_output.json")
    elif not raw_path.read_text(errors="replace").strip():
        problems.append("raw_output.json is empty")

    parsed_verdict = ""
    if isinstance(parsed, dict):
        if "parse_error" in parsed:
            problems.append(f"parse error recorded: {parsed.get('parse_error')}")
        parsed_verdict = str(parsed.get("verdict", ""))
        if not parsed_verdict:
            problems.append("parsed_result.json missing verdict")
    else:
        problems.append("parsed_result.json must be an object")

    if isinstance(judgement, dict):
        if judgement.get("evidence_bound") is not True:
            problems.append("judgement.evidence_bound is not true")
        if judgement.get("actionable") is not True:
            problems.append("judgement.actionable is not true")
    else:
        problems.append("judgement.json must be an object")

    if isinstance(packet, dict):
        for required in (
            "task_contract",
            "raw_state_candidates",
            "state_inspection_handles",
            "active_findings",
            "reason",
        ):
            if required not in packet:
                problems.append(f"verifier_packet missing {required}")
        forbidden = {
            "solver_claim",
            "submit_summary",
            "solver_proof",
            "privileged_solver_proof",
            "proof_contract",
            "proof_contract_analysis",
            "solver_authored_evidence",
            "recent_receipts",
            "artifact_history",
            "memory_events",
            "command_results",
            "latest_file_reads",
        }
        leaked = sorted(key for key in forbidden if key in packet)
        if leaked:
            problems.append(f"verifier_packet leaked solver journey fields: {leaked}")
    else:
        problems.append("verifier_packet.json must be an object")

    active_ids: list[str] = []
    if isinstance(findings_after, list):
        active_ids = [str(item.get("finding_id", "")) for item in findings_after if isinstance(item, dict) and item.get("finding_id")]
    else:
        problems.append("active_findings_after.json must be a list")

    # Case-specific expectations keep the gate meaningful without relying on model wording.
    if case in {"semantic_wrong", "missing_artifact", "schema_mismatch", "repeated_no_progress"}:
        if parsed_verdict != "needs_repair":
            problems.append(f"expected needs_repair, got {parsed_verdict!r}")
        if not active_ids:
            problems.append("expected at least one active finding")
    if case == "insufficient_evidence" and parsed_verdict != "uncertain_missing_evidence":
        problems.append(f"expected uncertain_missing_evidence, got {parsed_verdict!r}")
    if case == "solver_claim_conflicts_with_raw_state":
        if parsed_verdict != "uncertain_missing_evidence":
            problems.append(f"expected uncertain_missing_evidence, got {parsed_verdict!r}")
        if isinstance(packet, dict):
            candidates = packet.get("raw_state_candidates")
            if not isinstance(candidates, list) or not any(
                isinstance(item, dict)
                and item.get("path") == "data/events.log"
                and item.get("authority") == "candidate_only"
                for item in candidates
            ):
                problems.append("expected non-authoritative raw_state_candidate for data/events.log")
            if "solver_authored_evidence" in packet:
                problems.append("solver_authored_evidence must not be present in state-only verifier packet")

    return CaseValidation(case, not problems, tuple(problems), parsed_verdict, tuple(active_ids))


def _read_json(path: Path, problems: list[str], *, required: bool) -> Any:
    if not path.exists():
        if required:
            problems.append(f"missing {path.name}")
        return None
    try:
        return json.loads(path.read_text(errors="replace"))
    except Exception as exc:
        problems.append(f"invalid JSON in {path.name}: {exc}")
        return None


def _scan_for_secrets(out_dir: Path) -> list[str]:
    hits: list[str] = []
    for path in out_dir.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        text = path.read_text(errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(str(path.relative_to(out_dir)))
                break
    return hits[:20]


def _result(out_dir: Path, ok: bool, problems: list[str], cases: list[CaseValidation]) -> dict[str, Any]:
    return {
        "out_dir": str(out_dir),
        "ok": ok,
        "problems": list(problems),
        "cases": [item.as_dict() for item in cases],
    }


def _write_report(result: dict[str, Any], path: Path) -> None:
    lines = ["# Verifier-Only Evaluation Validation", "", f"Bundle: `{result['out_dir']}`", "", f"Overall: `{'PASS' if result['ok'] else 'FAIL'}`", ""]
    if result["problems"]:
        lines.extend(["## Bundle Problems", ""])
        for problem in result["problems"]:
            lines.append(f"- {problem}")
        lines.append("")
    lines.extend(["## Cases", "", "| case | ok | parsed verdict | active findings | problems |", "|---|---:|---|---|---|"])
    for case in result["cases"]:
        lines.append(
            f"| {case['case']} | {case['ok']} | {case['parsed_verdict']} | {case['active_findings']} | {case['problems']} |"
        )
    lines.append("")
    lines.append("This validator is offline-only. It does not call a model, solver, Docker, VM, benchmark, board, or official grader.")
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", help="Directory produced by run_verifier_only_eval.py")
    parser.add_argument("--allow-missing-cases", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    result = validate_bundle(Path(args.out_dir), require_all_cases=not args.allow_missing_cases)
    if args.report:
        _write_report(result, Path(args.report))
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
