#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def _load_ndjson(path: Path) -> list[dict]:
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _derive_expected_ticket(live_rows: list[dict]) -> dict[str, str]:
    candidates = [
        row
        for row in live_rows
        if str(row.get("status", "")).lower() == "open"
        and str(row.get("workspace_id", "")).startswith("ws-prod")
    ]
    if not candidates:
        raise ValueError("no open production tickets found in live snapshot")

    def sort_key(row: dict) -> tuple[int, str, str, str]:
        priority = row.get("priority", 10**9)
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 10**9
        return (
            priority,
            str(row.get("workspace_id", "")),
            str(row.get("ticket_id", "")),
            str(row.get("event_id", "")),
        )

    expected = sorted(candidates, key=sort_key)[0]
    return {
        "workspace_id": str(expected.get("workspace_id", "")),
        "ticket_id": str(expected.get("ticket_id", "")),
        "source_event_id": str(expected.get("event_id", "")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatch", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    dispatch_path = Path(args.dispatch).resolve()
    workspace_path = Path(args.workspace).resolve()
    workspace_root = Path(__file__).resolve().parent.parent

    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    workspace = workspace_path.read_text(encoding="utf-8").strip()

    reasons: list[str] = []
    for field in ["workspace_id", "ticket_id", "action_commands", "source_event_id"]:
        if field not in dispatch:
            reasons.append(f"missing_{field}")

    live_snapshot_path = workspace_root / "data/live_snapshot.ndjson"
    if not live_snapshot_path.exists():
        reasons.append("missing_live_snapshot")
        expected = None
    else:
        expected = _derive_expected_ticket(_load_ndjson(live_snapshot_path))

    final_submission_path = workspace_root / "out/final_submission.json"
    if not final_submission_path.exists():
        reasons.append("missing_final_submission")
        final_submission = None
    else:
        final_submission = json.loads(final_submission_path.read_text(encoding="utf-8"))

    if expected is not None:
        required_commands = [
            f"sync_ticket {expected['ticket_id']}",
            f"dispatch_workspace {expected['workspace_id']}",
        ]
        if dispatch.get("workspace_id") != expected["workspace_id"]:
            reasons.append("workspace_mismatch")
        if dispatch.get("ticket_id") != expected["ticket_id"]:
            reasons.append("ticket_mismatch")
        if dispatch.get("source_event_id") != expected["source_event_id"]:
            reasons.append("source_event_id_mismatch")
        if dispatch.get("action_commands") != required_commands:
            reasons.append("dispatch_commands_mismatch")
        if workspace != expected["workspace_id"]:
            reasons.append("workspace_selector_mismatch")

        if final_submission is not None:
            if final_submission.get("dispatch_plan_path") != "out/dispatch_plan.json":
                reasons.append("final_submission_dispatch_path_mismatch")
            if final_submission.get("selected_workspace_id") != expected["workspace_id"]:
                reasons.append("final_submission_workspace_mismatch")
            if final_submission.get("selected_ticket") != expected["ticket_id"]:
                reasons.append("final_submission_ticket_mismatch")
            if final_submission.get("verifier_command") != (
                "python3 scripts/visible_verify.py --dispatch out/dispatch_plan.json --workspace config/active_workspace.txt"
            ):
                reasons.append("final_submission_verifier_command_mismatch")

    result = {"passed": not reasons, "reason_codes": sorted(set(reasons))}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
