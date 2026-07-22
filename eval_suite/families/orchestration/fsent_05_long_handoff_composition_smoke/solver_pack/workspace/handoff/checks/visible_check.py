#!/usr/bin/env python3
import argparse
import json
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()

    candidate_path = Path(args.candidate).resolve()
    if not candidate_path.exists():
        print("missing candidate")
        return 1

    data = json.loads(candidate_path.read_text(encoding="utf-8"))
    required = ["artifact_id", "bundle_path", "bundle_sha256", "handoff_steps", "verifier_command"]
    missing = [field for field in required if field not in data]
    if missing:
        print(json.dumps({"passed": False, "missing": missing}, sort_keys=True))
        return 1

    workspace_root = candidate_path.parent.parent
    seed_path = workspace_root / "stage/inbox/artifact_seed.json"
    bundle_path = workspace_root / str(data["bundle_path"])

    reasons: list[str] = []
    if str(data["bundle_path"]) != "stage/final/handoff_bundle.json":
        reasons.append("bundle_path_mismatch")
    if str(data["verifier_command"]) != "python3 checks/visible_check.py --candidate out/final_submission.json":
        reasons.append("verifier_command_mismatch")
    if not isinstance(data.get("handoff_steps"), list) or len(data["handoff_steps"]) < 3:
        reasons.append("insufficient_handoff_steps")
    if not seed_path.exists():
        reasons.append("missing_seed_artifact")
    if not bundle_path.exists():
        reasons.append("missing_bundle")
    else:
        bundle_bytes = bundle_path.read_bytes()
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
        if data["bundle_sha256"] != bundle_sha:
            reasons.append("bundle_sha_mismatch")
        else:
            bundle = json.loads(bundle_bytes.decode("utf-8"))
            seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.exists() else {}
            if bundle.get("artifact_id") != seed.get("artifact_id"):
                reasons.append("bundle_artifact_id_mismatch")
            if bundle.get("payload") != seed.get("payload"):
                reasons.append("bundle_payload_mismatch")
            if bundle.get("source") != seed.get("source"):
                reasons.append("bundle_source_mismatch")
            if str(data["artifact_id"]) != str(seed.get("artifact_id", "")):
                reasons.append("artifact_id_mismatch")

    result = {"passed": not reasons, "reason_codes": sorted(set(reasons))}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
