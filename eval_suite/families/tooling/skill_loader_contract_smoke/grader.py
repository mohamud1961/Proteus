from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def grade_workspace(*, workspace_root: Path, reference_root: Path) -> dict[str, Any]:
    actual = _load_json(workspace_root / "skill_audit.json")
    expected = _load_json(reference_root / "skill_audit.json")
    reason_codes: list[str] = []

    if actual.get("skills_discovered") != expected.get("skills_discovered") or actual.get("skill_order_is_deterministic") is not True:
        reason_codes.append("skill_discovery_not_deterministic")

    if actual.get("frontmatter_summary") != expected.get("frontmatter_summary"):
        reason_codes.append("skill_frontmatter_parse_mismatch")

    if actual.get("collision_reason_codes") != expected.get("collision_reason_codes"):
        reason_codes.append("skill_collision_handling_missing")

    if actual.get("selection_reason_codes") != expected.get("selection_reason_codes"):
        reason_codes.append("skill_missing_reason_codes_absent")

    visible_context = actual.get("visible_context", {})
    expected_visible = expected.get("visible_context", {})
    if (
        visible_context.get("recorded_in_prefix") is not True
        or visible_context.get("contains_base_directory") is not True
        or visible_context.get("contains_skill_body") is not True
        or visible_context.get("bounded") is not True
        or visible_context != expected_visible
    ):
        reason_codes.append("skill_context_not_visible_or_bounded")

    if actual.get("metadata_retention") != expected.get("metadata_retention"):
        reason_codes.append("skill_metadata_not_retained_safely")

    hidden_behavior = actual.get("hidden_behavior", {})
    if (
        hidden_behavior.get("implicit_prefix_mutation") is not False
        or hidden_behavior.get("implicit_hook_execution") is not False
        or hidden_behavior.get("implicit_mcp_invocation") is not False
    ):
        reason_codes.append("hidden_prompt_mutation_detected")

    verdict = "pass" if not reason_codes else "fail"
    return {
        "verdict": verdict,
        "score": 1.0 if verdict == "pass" else 0.0,
        "reason_codes": reason_codes,
        "summary": "skill loader contract satisfied" if verdict == "pass" else "skill loader contract mismatch",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--reference-root", required=True)
    parser.add_argument("--mode", choices=("visible", "hidden"), default="visible")
    args = parser.parse_args()

    grade = grade_workspace(
        workspace_root=Path(args.workspace),
        reference_root=Path(args.reference_root),
    )
    print(json.dumps(grade, sort_keys=True))
    return 0 if grade["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
