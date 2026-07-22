#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def parse_env(path: Path):
    data = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_checkpoint_values(workspace_root: Path) -> dict[str, str]:
    checkpoint_a = _load_json(workspace_root / "data/checkpoints/checkpoint_a.json")
    checkpoint_b = _load_json(workspace_root / "data/checkpoints/checkpoint_b.json")
    checkpoint_c = _load_json(workspace_root / "data/checkpoints/checkpoint_c.json")
    handoff_manifest = _load_json(workspace_root / "ops/release/handoff_manifest.json")
    chat_excerpt = (workspace_root / "reports/incident/ops_chat_excerpt.log").read_text(encoding="utf-8")

    return {
        "bundle_path": str(handoff_manifest.get("bundle_output_path", "artifacts/recovery/final/recovery_bundle.json")),
        "runtime_host": str(checkpoint_a.get("runtime_host_candidate", "")) or str(checkpoint_b.get("runtime_host_candidate", "")),
        "queue_name": str(checkpoint_b.get("queue_name", "")),
        "token_suffix": str(checkpoint_a.get("token_suffix", "")),
        "commit_marker": str(checkpoint_a.get("commit_marker", "")) or str(checkpoint_c.get("commit_marker", "")),
        "closure_ticket": f"RCV-2026-{str(checkpoint_c.get('closure_ticket_suffix', '')).strip()}",
        "runtime_mode": "hardened" if "hardened" in chat_excerpt.lower() else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    workspace_root = Path(__file__).resolve().parent.parent
    bundle_path = Path(args.bundle)
    if not bundle_path.is_absolute():
        bundle_path = (workspace_root / bundle_path).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (workspace_root / config_path).resolve()

    bundle = _load_json(bundle_path)
    env = parse_env(config_path)
    expected = _read_checkpoint_values(workspace_root)
    final_submission_path = workspace_root / "out/final_submission.json"

    reasons: list[str] = []
    for k in ["closure_ticket", "runtime_host", "queue_name", "token_suffix", "commit_marker"]:
        if k not in bundle:
            reasons.append(f"missing_bundle_field_{k}")

    if not final_submission_path.exists():
        reasons.append("missing_final_submission")
        final_submission = {}
    else:
        final_submission = _load_json(final_submission_path)

    if bundle.get("closure_ticket") != expected["closure_ticket"]:
        reasons.append("bundle_closure_ticket_mismatch")
    if bundle.get("runtime_host") != expected["runtime_host"]:
        reasons.append("bundle_runtime_host_mismatch")
    if bundle.get("queue_name") != expected["queue_name"]:
        reasons.append("bundle_queue_name_mismatch")
    if bundle.get("token_suffix") != expected["token_suffix"]:
        reasons.append("bundle_token_suffix_mismatch")
    if bundle.get("commit_marker") != expected["commit_marker"]:
        reasons.append("bundle_commit_marker_mismatch")

    if env.get("RUNTIME_HOST") != expected["runtime_host"]:
        reasons.append("runtime_host_env_mismatch")
    if env.get("RUNTIME_PORT") != "9100":
        reasons.append("runtime_port_env_mismatch")
    if env.get("QUEUE_NAME") != expected["queue_name"]:
        reasons.append("queue_name_env_mismatch")
    if env.get("RECOVERY_TOKEN") != f"token-recovery-{expected['token_suffix']}":
        reasons.append("recovery_token_env_mismatch")
    if env.get("RECOVERY_MODE") != expected["runtime_mode"]:
        reasons.append("recovery_mode_env_mismatch")

    if final_submission:
        if final_submission.get("output_path") != expected["bundle_path"]:
            reasons.append("candidate_output_path_mismatch")
        if final_submission.get("closure_ticket") != expected["closure_ticket"]:
            reasons.append("candidate_closure_ticket_mismatch")
        if final_submission.get("runtime_host") != expected["runtime_host"]:
            reasons.append("candidate_runtime_host_mismatch")
        if final_submission.get("queue_name") != expected["queue_name"]:
            reasons.append("candidate_queue_name_mismatch")
        if final_submission.get("verifier_command") != (
            "python3 scripts/verify_recovery.py --bundle artifacts/recovery/final/recovery_bundle.json --config services/catalog/config/runtime.env"
        ):
            reasons.append("candidate_verifier_command_mismatch")

    print("VISIBLE_PASS" if not reasons else "VISIBLE_FAIL " + ",".join(sorted(set(reasons))))
    return 0 if not reasons else 2


if __name__ == "__main__":
    raise SystemExit(main())
