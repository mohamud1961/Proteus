from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _project_trace(trace: dict[str, Any] | None) -> dict[str, Any] | None:
    if trace is None:
        return None
    return {
        "discovery": trace.get("discovery"),
        "schema_mapping": trace.get("schema_mapping"),
        "invocation_results": trace.get("invocation_results"),
        "hook_permission_trace": trace.get("hook_permission_trace"),
        "native_regression": trace.get("native_regression"),
        "challenge_response": trace.get("challenge_response"),
    }


def grade_workspace(*, workspace_root: Path, reference_root: Path, mode: str = "visible") -> dict[str, Any]:
    candidate = _read_json(workspace_root / "mcp_audit.json")
    reference = _read_json(reference_root / "mcp_audit.json")
    candidate_trace = _read_json(workspace_root / "mcp_registry_trace.json")
    reference_trace = _read_json(reference_root / "mcp_registry_trace.json")
    reason_codes: list[str] = []

    if candidate is None:
        reason_codes.append("missing_mcp_audit")
    if reference is None:
        reason_codes.append("missing_reference_audit")
    if candidate_trace is None:
        reason_codes.append("missing_mcp_registry_trace")
    if reference_trace is None:
        reason_codes.append("missing_reference_trace")

    checks = {
        "candidate_present": candidate is not None,
        "reference_present": reference is not None,
        "candidate_trace_present": candidate_trace is not None,
        "reference_trace_present": reference_trace is not None,
    }

    # Challenge validation
    challenge_path = workspace_root / "challenge.txt"
    if challenge_path.exists():
        challenge_token = challenge_path.read_text(encoding="utf-8").strip()
    else:
        challenge_token = "default_smoke_challenge"
    expected_challenge_response = f"payload={challenge_token}"

    if candidate is not None:
        if candidate.get("challenge_response") != expected_challenge_response:
            reason_codes.append("mcp_challenge_response_invalid_or_missing")

    if candidate_trace is not None:
        if candidate_trace.get("challenge_response") != expected_challenge_response:
            reason_codes.append("mcp_trace_challenge_response_invalid_or_missing")

    if candidate is not None and reference is not None:
        candidate_bundle = candidate.get("bundle_contract")
        reference_bundle = reference.get("bundle_contract")

        if candidate.get("discovery") != reference.get("discovery"):
            reason_codes.append("mcp_discovery_not_deterministic")
        if candidate.get("schema_mapping") != reference.get("schema_mapping"):
            reason_codes.append("mcp_schema_mapping_not_faithful")
        if candidate.get("invocation_results") != reference.get("invocation_results"):
            reason_codes.append("mcp_typed_results_mismatch")
        if candidate.get("hook_permission_trace") != reference.get("hook_permission_trace"):
            reason_codes.append("mcp_hook_permission_evidence_missing")
        if candidate.get("native_regression") != reference.get("native_regression"):
            reason_codes.append("native_tool_regression_detected")
        if candidate_bundle != reference_bundle:
            reason_codes.append("mcp_bundle_contract_mismatch")

    if candidate is not None and candidate_trace is not None:
        if _project_trace(candidate_trace) != {
            "discovery": candidate.get("discovery"),
            "schema_mapping": candidate.get("schema_mapping"),
            "invocation_results": candidate.get("invocation_results"),
            "hook_permission_trace": candidate.get("hook_permission_trace"),
            "native_regression": candidate.get("native_regression"),
            "challenge_response": candidate.get("challenge_response"),
        }:
            reason_codes.append("mcp_trace_bundle_mismatch")

    verdict = "pass" if not reason_codes else "fail"
    score = 1.0 if verdict == "pass" else 0.0
    return {
        "mode": mode,
        "workspace_root": str(workspace_root),
        "reference_root": str(reference_root),
        "verdict": verdict,
        "score": score,
        "reason_codes": sorted(set(reason_codes)),
        "checks": checks,
        "candidate": candidate,
        "reference": reference,
        "candidate_trace": candidate_trace,
        "reference_trace": reference_trace,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade the MCP registry contract smoke workspace.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--reference-root", required=True)
    parser.add_argument("--mode", choices=("visible", "hidden"), default="visible")
    args = parser.parse_args(argv)

    result = grade_workspace(
        workspace_root=Path(args.workspace),
        reference_root=Path(args.reference_root),
        mode=args.mode,
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
