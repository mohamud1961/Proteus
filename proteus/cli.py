from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from proteus.compiler import Compiler
from proteus.evidence import EvidenceLedger
from proteus.models import Deliverable, RunResult, SolverAction, TaskSpec
from proteus.runtime import WorkspaceExecutor
from proteus.solver import ReplaySolver
from proteus.verifier import IndependentVerifier


def demo_task() -> TaskSpec:
    return TaskSpec(
        task_id="hello-artifact-v1",
        prompt="Create a runnable Python artifact that prints the requested greeting.",
        deliverables=(Deliverable("hello_app.py", 'print("hello from Proteus")\n'),),
        verification_command=(sys.executable, "hello_app.py"),
        verification_args=(),
    )


def demo_actions(task: TaskSpec) -> tuple[SolverAction, ...]:
    deliverable = task.deliverables[0]
    return (
        SolverAction("solver-001", "write_file", path=deliverable.path, content=deliverable.expected_text, rationale="materialize the requested artifact"),
        SolverAction("solver-002", "read_file", path=deliverable.path, rationale="inspect the saved artifact"),
        SolverAction("solver-003", "run_command", command=task.verification_command, rationale="exercise the artifact before submission"),
    )


def run_replay(output_dir: Path | None = None) -> RunResult:
    task = demo_task()
    temporary = output_dir is None
    context = tempfile.TemporaryDirectory(prefix="proteus-demo-") if temporary else None
    root = Path(context.name) if context else output_dir.resolve()  # type: ignore[union-attr]
    root.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceLedger(root / "proteus-evidence.jsonl")
    workbench = Compiler().compile(task, root)
    evidence.record("workbench_compiled", {"workbench": workbench.as_dict(), "provider": "replay"})
    solver = ReplaySolver(demo_actions(task))
    executor = WorkspaceExecutor(root, evidence)
    receipts = tuple(executor.execute(action) for action in solver.actions())
    verification = IndependentVerifier().verify(task, root)
    evidence.record("verification", verification.as_dict())
    result = RunResult(task.task_id, "replay", workbench, receipts, verification, str(root / "proteus-evidence.jsonl"), str(root))
    if temporary:
        # The temporary run is useful for tests; the CLI uses a retained output directory.
        context.cleanup()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Proteus replay demo")
    parser.add_argument("command", choices=("demo",), nargs="?", default="demo")
    parser.add_argument("--output-dir", type=Path, default=Path(".proteus-demo"))
    args = parser.parse_args(argv)
    result = run_replay(args.output_dir)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.verification.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
