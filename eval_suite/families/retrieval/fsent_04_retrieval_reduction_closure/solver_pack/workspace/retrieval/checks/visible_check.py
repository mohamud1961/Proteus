#!/usr/bin/env python3
import argparse
import json
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
    required = ["final_value", "evidence_ids", "rejected_stale_ids", "justification_trace"]
    missing = [field for field in required if field not in data]
    reasons = []

    if missing:
        reasons.append(f"missing_fields: {missing}")
    else:
        seed_root = Path(__file__).resolve().parents[1]
        evidence_path = seed_root / "data" / "evidence_rows.jsonl"
        active_ids = []
        stale_ids = []
        final_value = 0
        for line in evidence_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            status = row.get("status")
            evidence_id = row.get("evidence_id")
            if status == "active":
                active_ids.append(evidence_id)
                final_value += int(row["value"])
            elif status == "stale":
                stale_ids.append(evidence_id)

        if data.get("final_value") != final_value:
            reasons.append("final_value_mismatch")
        if data.get("evidence_ids") != active_ids:
            reasons.append("missing_required_evidence_id")
        if data.get("rejected_stale_ids") != stale_ids:
            reasons.append("stale_id_not_rejected")

        trace = data.get("justification_trace", {})
        if not isinstance(trace, dict):
            reasons.append("justification_trace_not_dict")
        else:
            all_ids = active_ids + stale_ids
            missing_justifications = [
                evidence_id
                for evidence_id in all_ids
                if not isinstance(trace.get(evidence_id), str)
                or len(trace[evidence_id].strip()) < 10
            ]
            if missing_justifications:
                reasons.append(f"missing_justifications_for_ids: {missing_justifications}")

    if reasons:
        print(json.dumps({"passed": False, "reasons": reasons}, sort_keys=True))
        return 1

    print(json.dumps({"passed": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
