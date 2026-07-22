"""runner CLI — run an eval pack offline with a deterministic local stub agent.

Usage:
    python -m runner run-eval <task_pack_path>

The task pack must be a JSON file referencing fixture, grader, and task metadata.
The LocalStubModelClient runs fully offline with zero API credentials.

Currently demonstrated eval pack:
    eval_suite/families/tooling/mcp_registry_contract_smoke/task_pack.json
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
_RUNNER_DIR = Path(__file__).resolve().parent
REPO_ROOT = _RUNNER_DIR.parent


# ---------------------------------------------------------------------------
# Local stub agent
# ---------------------------------------------------------------------------

class LocalStubModelClient:
    """Offline deterministic agent for offline eval runs.

    Reads the fixture/reference artefacts for the given pack and writes the
    correct output files to the workspace — no API credentials required.
    """

    def __init__(self, pack: dict[str, Any], fixture_dir: Path) -> None:
        self._pack = pack
        self._fixture_dir = fixture_dir

    def solve(self, workspace_dir: Path) -> dict[str, Any]:
        """Write correct solution files to workspace_dir; return agent result."""
        fixture_ref = self._pack.get("fixture", {})
        fixture_type = fixture_ref.get("type", "")

        if fixture_type == "synthetic_mcp_registry_contract":
            return self._solve_mcp_registry(workspace_dir)

        # Generic fallback: copy every file from fixture workspace dir
        fixture_workspace = self._fixture_dir / fixture_ref.get("workspace_dir", "workspace")
        if fixture_workspace.is_dir():
            for src in fixture_workspace.rglob("*"):
                if src.is_file():
                    rel = src.relative_to(fixture_workspace)
                    dst = workspace_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            return {
                "status": "stub_copied_fixture",
                "files_written": [
                    str(f.relative_to(workspace_dir))
                    for f in workspace_dir.rglob("*")
                    if f.is_file()
                ],
            }

        return {"status": "stub_noop", "files_written": []}

    def _solve_mcp_registry(self, workspace_dir: Path) -> dict[str, Any]:
        """Write mcp_audit.json and mcp_registry_trace.json from reference."""
        reference_dir = self._fixture_dir / "reference"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # Default challenge response (no challenge.txt in workspace)
        challenge_response = "payload=default_smoke_challenge"

        files_written: list[str] = []
        for fname in ("mcp_audit.json", "mcp_registry_trace.json"):
            ref_path = reference_dir / fname
            if not ref_path.exists():
                continue
            data = json.loads(ref_path.read_text(encoding="utf-8"))
            # Add challenge_response so grader's challenge check passes
            data["challenge_response"] = challenge_response
            dst = workspace_dir / fname
            dst.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            files_written.append(fname)

        return {
            "status": "stub_wrote_reference_solution",
            "files_written": files_written,
            "challenge_response": challenge_response,
        }


# ---------------------------------------------------------------------------
# Grade dispatcher
# ---------------------------------------------------------------------------

def _load_grader_module(grader_py: Path):
    """Dynamically load the grader Python module from the pack directory."""
    spec = importlib.util.spec_from_file_location("_pack_grader", grader_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load grader from {grader_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_grader(
    *,
    pack: dict[str, Any],
    pack_dir: Path,
    workspace_dir: Path,
) -> dict[str, Any]:
    """Dispatch grading based on the pack's grader type."""
    fixture_ref = pack.get("fixture", {})
    fixture_type = fixture_ref.get("type", "")

    if fixture_type == "synthetic_mcp_registry_contract":
        reference_dir = pack_dir / "fixture" / fixture_ref.get("reference_dir", "reference")
        grader_py = pack_dir / "grader.py"
        grader_mod = _load_grader_module(grader_py)
        result = grader_mod.grade_workspace(
            workspace_root=workspace_dir,
            reference_root=reference_dir,
            mode="visible",
        )
        verdict = result.get("verdict", "fail")
        score = float(result.get("score", 1.0 if verdict == "pass" else 0.0))
        return {
            "verdict": verdict,
            "score": score,
            "reason_codes": result.get("reason_codes", []),
            "grader_detail": result,
        }

    # Generic: look for a grader.py with grade_workspace()
    grader_py = pack_dir / "grader.py"
    if grader_py.exists():
        grader_mod = _load_grader_module(grader_py)
        if hasattr(grader_mod, "grade_workspace"):
            result = grader_mod.grade_workspace(workspace_root=workspace_dir)
            verdict = result.get("verdict", "fail")
            score = float(result.get("score", 1.0 if verdict == "pass" else 0.0))
            return {
                "verdict": verdict,
                "score": score,
                "reason_codes": result.get("reason_codes", []),
                "grader_detail": result,
            }

    return {
        "verdict": "fail",
        "score": 0.0,
        "reason_codes": ["no_grader_available"],
        "grader_detail": {},
    }


# ---------------------------------------------------------------------------
# run-eval command
# ---------------------------------------------------------------------------

def run_eval(task_pack_path: str) -> dict[str, Any]:
    """Run an eval pack offline with the LocalStubModelClient.

    Args:
        task_pack_path: Path to the task_pack.json file (or directory containing it).

    Returns:
        Dict with task_id, passed, score, and detail.
    """
    pack_path = Path(task_pack_path).resolve()
    if pack_path.is_dir():
        # Accept directory; look for task_pack.json or task_pack.yaml
        for name in ("task_pack.json", "task_pack.yaml"):
            candidate = pack_path / name
            if candidate.exists():
                pack_path = candidate
                break
        else:
            raise FileNotFoundError(
                f"No task_pack.json or task_pack.yaml found in {pack_path}"
            )

    if not pack_path.exists():
        raise FileNotFoundError(f"task pack not found: {pack_path}")

    pack_dir = pack_path.parent
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    task_id = pack.get("task_id", pack_path.stem)

    # Resolve fixture directory
    fixture_ref = pack.get("fixture", {})
    fixture_workspace_ref = fixture_ref.get("workspace_ref", "")
    if fixture_workspace_ref.startswith("/"):
        # absolute-style ref relative to repo root
        fixture_dir = REPO_ROOT / fixture_workspace_ref.lstrip("/")
        fixture_dir = fixture_dir.parent  # strip the workspace/ leaf; we want the family dir
    else:
        fixture_dir = pack_dir / "fixture"

    with tempfile.TemporaryDirectory(prefix="runner_eval_") as tmp:
        workspace_dir = Path(tmp) / "workspace"
        workspace_dir.mkdir()

        # Run stub agent
        stub = LocalStubModelClient(pack=pack, fixture_dir=fixture_dir if fixture_dir.exists() else pack_dir / "fixture")
        agent_result = stub.solve(workspace_dir)

        # Grade
        grade = _run_grader(
            pack=pack,
            pack_dir=pack_dir,
            workspace_dir=workspace_dir,
        )

    passed = grade["verdict"] == "pass"
    score = grade["score"]

    return {
        "task_id": task_id,
        "passed": passed,
        "score": score,
        "verdict": grade["verdict"],
        "reason_codes": grade["reason_codes"],
        "agent_result": agent_result,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if not args:
        print("Usage: python -m runner run-eval <task_pack_path>", file=sys.stderr)
        return 2

    command = args[0]

    if command == "run-eval":
        if len(args) < 2:
            print("Usage: python -m runner run-eval <task_pack_path>", file=sys.stderr)
            return 2
        task_pack_path = args[1]
        try:
            result = run_eval(task_pack_path)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1

    print(f"unknown command: {command}", file=sys.stderr)
    print("Available commands: run-eval", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
