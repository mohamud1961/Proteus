#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import re
import runpy

GRADER_TIMEOUT_SECONDS = 60
FORBIDDEN_TOKENS = ("reviewer_pack", "hidden_truth")


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _dedupe(items):
    out = []
    seen = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _timeout_reason_codes(trace, policy):
    if not bool(trace.get("meta", {}).get("timed_out", False)):
        return [], {
            "timeout_detected": False,
            "timeout_bucket": None,
            "classified_as_invalid_environment": False
        }
    if bool(trace.get("meta", {}).get("infrastructure_timeout", False)):
        return ["invalid_environment_timeout"], {
            "timeout_detected": True,
            "timeout_bucket": "infra_timeout",
            "classified_as_invalid_environment": True
        }
    retry_threshold = int(policy.get("retry_loop_threshold", 3))
    failed_calls = [
        e for e in trace.get("events", [])
        if str(e.get("event_type")) == "tool_call" and int(e.get("exit_code", 0)) != 0
    ]
    if len(failed_calls) >= retry_threshold:
        return ["timeout_step_budget_exhausted"], {
            "timeout_detected": True,
            "timeout_bucket": "step_budget",
            "classified_as_invalid_environment": False
        }
    return ["timeout_without_completion"], {
        "timeout_detected": True,
        "timeout_bucket": "generic",
        "classified_as_invalid_environment": False
    }


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
        token_fragments = [frag for frag in fragments if token in frag]
        if token_fragments and all(_is_exclusion_only_fragment(frag, token) for frag in token_fragments):
            continue
        return True
    return False


def _inspect_forbidden_events(trace):
    reasons = []
    for event in trace.get("events", []):
        path = str(event.get("path", "")).lower()
        cmd = str(event.get("command", ""))
        if any(token in path for token in FORBIDDEN_TOKENS):
            reasons.append("hidden_truth_access_attempt")
            continue
        if _has_forbidden_access_intent(cmd):
            reasons.append("hidden_truth_access_attempt")
    return sorted(set(reasons))


def _classify_failure(reason_codes, fallback):
    mapping = {
        "missing_recovery_bundle": "verification_completion",
        "runtime_env_mismatch": "environment_toolchain",
        "bundle_field_mismatch": "retrieval_reduction",
        "candidate_output_path_mismatch": "filesystem_path",
        "missing_verifier_pass_event": "verification_completion",
        "insufficient_long_horizon_trace": "long_horizon_orchestration",
        "hidden_truth_access_attempt": "contamination",
        "invalid_environment_timeout": "invalid_environment"
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
    hidden_truth_path = seed_root / "reviewer_pack" / "hidden_truth.json"
    hidden_verifier_path = seed_root / "reviewer_pack" / "hidden_verifier.py"
    timeout_policy_path = seed_root / "grader" / "timeout_policy.json"

    candidate_path = Path(args.candidate).resolve()
    trace_path = Path(args.trace).resolve()
    output_path = Path(args.output).resolve()

    trace = _load_json(trace_path)
    timeout_policy = _load_json(timeout_policy_path)

    module = runpy.run_path(str(hidden_verifier_path))
    evaluate = module["evaluate"]
    base = evaluate(hidden_truth_path, candidate_path, trace)

    timeout_reasons, timeout_meta = _timeout_reason_codes(trace, timeout_policy)
    forbidden_reasons = _inspect_forbidden_events(trace)
    reason_codes = _dedupe(base.get("reason_codes", []) + timeout_reasons + forbidden_reasons)

    verdict = "pass"
    score = 1.0
    failure_class = "none"
    if reason_codes:
        verdict = "fail"
        score = 0.0
        failure_class = _classify_failure(reason_codes, base.get("failure_class", "verification_completion"))

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
        "timeout_coverage": timeout_meta,
        "timeout_policy_applied": "grader/timeout_policy.json"
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
