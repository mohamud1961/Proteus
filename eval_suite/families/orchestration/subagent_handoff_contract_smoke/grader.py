from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def grade_workspace(*, workspace_root: Path, reference_root: Path) -> dict[str, Any]:
    actual = _load_json(workspace_root / "subagent_audit.json")
    expected = _load_json(reference_root / "subagent_audit.json")
    reason_codes: list[str] = []

    if actual.get("agents_loaded") != expected.get("agents_loaded") or actual.get("agent_order_is_deterministic") is not True:
        reason_codes.append("agent_loader_not_deterministic")

    if actual.get("frontmatter_summary") != expected.get("frontmatter_summary"):
        reason_codes.append("agent_frontmatter_parse_mismatch")

    if actual.get("resolution_summary") != expected.get("resolution_summary"):
        reason_codes.append("skill_or_mcp_refs_not_visible")

    if actual.get("task_packet") != expected.get("task_packet"):
        reason_codes.append("task_packet_contract_missing")

    if actual.get("handoff") != expected.get("handoff"):
        reason_codes.append("handoff_contract_missing")

    visibility = actual.get("visibility", {})
    expected_visibility = expected.get("visibility", {})
    if visibility.get("parent_visible_unresolved_risks") is not True or visibility != expected_visibility:
        reason_codes.append("unresolved_risks_hidden")

    if visibility.get("no_silent_background_execution") is not True:
        reason_codes.append("silent_background_execution_assumed")

    verdict = "pass" if not reason_codes else "fail"
    return {
        "verdict": verdict,
        "score": 1.0 if verdict == "pass" else 0.0,
        "reason_codes": reason_codes,
        "summary": "subagent handoff contract satisfied" if verdict == "pass" else "subagent handoff contract mismatch",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--reference-root", required=True)
    parser.add_argument("--mode", choices=("visible", "hidden"), default="visible")
    args = parser.parse_args()

    grade = grade_workspace(workspace_root=Path(args.workspace), reference_root=Path(args.reference_root))
    print(json.dumps(grade, sort_keys=True))
    return 0 if grade["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
