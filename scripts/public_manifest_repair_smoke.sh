#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

OUTPUT_ROOT="${1:-${TMPDIR:-/private/tmp}/harnesseng_public_manifest_smoke}"
rm -rf "$OUTPUT_ROOT"
mkdir -p "$OUTPUT_ROOT"

python3 - "$REPO_ROOT" "$OUTPUT_ROOT" <<'PY'
import json
import shutil
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
output_root = Path(sys.argv[2])
family_root = repo_root / "eval_suite" / "families" / "filesystem" / "public_manifest_repair_smoke"
task_pack_path = family_root / "task_pack.json"
board_path = repo_root / "eval_suite" / "boards" / "public_manifest_repair_smoke_v1.json"
grader_path = family_root / "grader.py"
fixture_root = family_root / "fixture"


def load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return payload


def load_grader_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("public_manifest_repair_smoke_grader", grader_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load public manifest repair grader")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


task_pack = load_json(task_pack_path)
if task_pack.get("task_id") != "public_manifest_repair_smoke_v1":
    raise SystemExit("unexpected public manifest smoke task id")
board = load_json(board_path)
grader = load_grader_module()

pass_root = output_root / "pass"
fail_root = output_root / "known_bad"
copy_tree(fixture_root / "workspace", pass_root / "workspace")
copy_tree(fixture_root / "reference", pass_root / "reference")
copy_tree(fixture_root / "workspace", fail_root / "workspace")
copy_tree(fixture_root / "reference", fail_root / "reference")

pass_reference = pass_root / "reference"
pass_workspace_release = pass_root / "workspace" / "release"
pass_workspace_release.joinpath("manifest.json").write_text((pass_reference / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
pass_workspace_release.joinpath("summary.txt").write_text((pass_reference / "summary.txt").read_text(encoding="utf-8"), encoding="utf-8")
pass_workspace_release.joinpath("checksum.txt").write_text((pass_reference / "checksum.txt").read_text(encoding="utf-8"), encoding="utf-8")

grades = {
    "pass": grader.grade_workspace(workspace_root=pass_root / "workspace", reference_root=pass_reference, mode="visible"),
    "known_bad": grader.grade_workspace(workspace_root=fail_root / "workspace", reference_root=fail_root / "reference", mode="visible"),
}

result_rows: list[dict[str, object]] = []
for label, grade in grades.items():
    artifact_root = output_root / label
    write_json(artifact_root / "grader_output.json", grade)
    write_json(
        artifact_root / "environment_manifest.json",
        {
            "sandbox_type": "debug_local_no_sandbox",
            "python_command": "python3",
            "cwd": str(repo_root),
            "fixture_root_ref": str(fixture_root.relative_to(repo_root)),
            "board_ref": str(board_path.relative_to(repo_root)),
            "task_pack_ref": str(task_pack_path.relative_to(repo_root)),
            "reference_root_ref": str((artifact_root / "reference").relative_to(output_root)),
        },
    )
    write_json(
        artifact_root / "artifact_bundle.json",
        {
            "board_id": board["board_id"],
            "task_pack_id": task_pack["task_id"],
            "workspace_ref": f"{label}/workspace",
            "reference_ref": f"{label}/reference",
            "grader_output_ref": f"{label}/grader_output.json",
            "grade": grade,
        },
    )
    write_json(
        artifact_root / "trace.json",
        {
            "board_id": board["board_id"],
            "run_label": label,
            "mode": "public_smoke_example",
            "tool_calls": [],
            "notes": "No model call. Deterministic grader-only public smoke.",
        },
    )
    result_rows.append(
        {
            "run_id": f"public_manifest_repair_smoke_{label}",
            "eval_id": "public_manifest_repair_smoke_v1",
            "task_pack_id": "public_manifest_repair_smoke_v1",
            "family": "public_manifest_repair_smoke",
            "surface_type": "verifier_repair",
            "admission_level": "diagnostic",
            "backend_ref": "debug_local_no_sandbox",
            "environment_ref": f"{label}/environment_manifest.json",
            "artifact_refs": [f"{label}/artifact_bundle.json"],
            "trace_refs": [f"{label}/trace.json"],
            "closure_status": "closed",
            "task_truth_status": "pass" if grade["verdict"] == "pass" else "fail",
            "contamination_status": "clean",
            "failure_class": "none" if grade["verdict"] == "pass" else "verification_grading",
            "reason_codes": list(grade["reason_codes"]),
            "verifier_ref": str(grader_path.relative_to(repo_root)),
            "grader_ref": f"{label}/grader_output.json",
            "score": float(grade["score"]),
        }
    )

scoreboard = {
    "totals": {
        "pass": sum(1 for grade in grades.values() if grade["verdict"] == "pass"),
        "fail": sum(1 for grade in grades.values() if grade["verdict"] == "fail"),
        "invalid": 0,
        "total": len(grades),
    }
}
output = {
    "board_id": board["board_id"],
    "example_only": True,
    "scope_label": "public_smoke_example_not_benchmark_evidence",
    "result_rows": result_rows,
    "scoreboard": scoreboard,
}
write_json(output_root / "public_manifest_repair_smoke_example.json", output)
print(output_root / "public_manifest_repair_smoke_example.json")
PY
