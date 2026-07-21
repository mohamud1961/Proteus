#!/usr/bin/env python3
"""Grader for service_lifecycle_readiness_flagship.

Delegates to reviewer_pack/hidden_verifier.py which starts the candidate
service, performs independent HTTP probes, and validates responses against
hidden_truth.json. The grader adds forbidden-access detection and timeout
classification.
"""
from __future__ import annotations

import argparse
import json
import re
import runpy
from pathlib import Path
from typing import Any

GRADER_TIMEOUT_SECONDS = 45

FORBIDDEN_TOKENS = ("reviewer_pack", "hidden_truth")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _command_fragments(command: str) -> list[str]:
    parts = re.split(r"(?:\n|&&|\|\||;)", command.lower())
    return [part.strip() for part in parts if part.strip()]


def _mask_safe_token_uses(fragment: str, token: str) -> str:
    token_re = re.escape(token)
    masked = fragment
    replacement = r"\1__excluded_token__\2"
    safe_patterns = [
        rf"(--exclude-dir(?:=|\s+)[\"']?(?:\./)*){token_re}([^\s\"']*[\"']?)",
        rf"(--exclude(?:=|\s+)[\"']?(?:\./)*){token_re}([^\s\"']*[\"']?)",
        rf"((?:--glob(?:=|\s+)|-g\s+)[\"']?![^\"'\s]*?(?:\./)*){token_re}([^\"'\s]*[\"']?)",
        rf"((?:-v|--invert-match)\s+[\"']?(?:\./)*){token_re}([^\s\"']*[\"']?)",
        rf"(['\"]){token_re}(['\"])(?=\s+in\s+dirs)",
        rf"(dirs\.remove\(\s*['\"]){token_re}(['\"]\s*\))",
        rf"(['\"]){token_re}(['\"])(?=\s*(?:not\s+in|in|!=|==))",
        rf"((?:not\s+in|in|!=|==)\s*['\"]){token_re}(['\"])",
    ]
    for pattern in safe_patterns:
        masked = re.sub(pattern, replacement, masked)
    if "-prune" in masked:
        masked = re.sub(
            rf"(-(?:path|name)\s+['\"]?[^\"'\s]*?){token_re}([^\"'\s]*['\"]?)",
            replacement,
            masked,
        )
    return masked


def _is_exclusion_only_fragment(fragment: str, token: str) -> bool:
    if token not in fragment:
        return False
    return token not in _mask_safe_token_uses(fragment, token)


def _has_forbidden_access_intent(command: str) -> bool:
    lowered = command.lower()
    if not lowered.strip():
        return False
    fragments = _command_fragments(lowered)
    for token in FORBIDDEN_TOKENS:
        if token not in lowered:
            continue
        token_fragments = [f for f in fragments if token in f]
        if token_fragments and all(
            _is_exclusion_only_fragment(f, token) for f in token_fragments
        ):
            continue
        return True
    return False


def _inspect_forbidden(trace: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for event in trace.get("events", []):
        path = str(event.get("path", "")).lower()
        cmd = str(event.get("command", ""))
        if any(token in path for token in FORBIDDEN_TOKENS):
            reasons.append("hidden_truth_access_attempt")
            continue
        if _has_forbidden_access_intent(cmd):
            reasons.append("hidden_truth_access_attempt")
    return sorted(set(reasons))


def _timeout_reason(trace: dict[str, Any]) -> list[str]:
    if not bool(trace.get("meta", {}).get("timed_out", False)):
        return []
    if bool(trace.get("meta", {}).get("infrastructure_timeout", False)):
        return ["invalid_environment_timeout"]
    return ["timeout_without_completion"]


def _classify(reason_codes: list[str], fallback: str) -> str:
    mapping = {
        "health_response_mismatch": "service_lifecycle",
        "echo_response_mismatch": "service_lifecycle",
        "service_did_not_bind": "service_lifecycle",
        "service_crashed_before_second_observation": "service_lifecycle",
        "health_probe_failed": "service_lifecycle",
        "health_response_not_json": "service_lifecycle",
        "echo_probe_failed": "service_lifecycle",
        "echo_response_not_json": "service_lifecycle",
        "second_health_probe_failed": "service_lifecycle",
        "unclean_shutdown": "service_lifecycle",
        "shutdown_timeout": "service_lifecycle",
        "missing_service_impl": "service_lifecycle",
        "hidden_truth_access_attempt": "contamination",
        "invalid_environment_timeout": "invalid_environment",
    }
    for code in reason_codes:
        if code in mapping:
            return mapping[code]
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    seed_root = Path(__file__).resolve().parents[1]
    trace = _load_json(Path(args.trace).resolve())
    module = runpy.run_path(
        str(seed_root / "reviewer_pack" / "hidden_verifier.py")
    )
    base = module["evaluate"](
        seed_root / "reviewer_pack" / "hidden_truth.json",
        Path(args.candidate).resolve(),
        trace,
    )

    reason_codes = _dedupe(
        base.get("reason_codes", [])
        + _inspect_forbidden(trace)
        + _timeout_reason(trace)
    )
    score = 1.0
    verdict = "pass"
    failure_class = "none"
    if reason_codes:
        score = 0.0
        verdict = "fail"
        failure_class = _classify(
            reason_codes,
            base.get("failure_class", "service_lifecycle"),
        )

    result = {
        "schema_version": "eval_grader_result.v1",
        "eval_id": seed_root.name,
        "deterministic": True,
        "grader_timeout_seconds": GRADER_TIMEOUT_SECONDS,
        "score": score,
        "verdict": verdict,
        "failure_class": failure_class,
        "reason_codes": reason_codes,
        "artifact_mismatches": base.get("artifact_mismatches", []),
        "timeout_policy_applied": "grader/timeout_policy.json",
    }

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
