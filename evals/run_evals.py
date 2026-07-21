from __future__ import annotations

from pathlib import Path
import tempfile

from proteus.cli import demo_actions, demo_task
from proteus.compiler import Compiler
from proteus.evidence import EvidenceLedger
from proteus.runtime import WorkspaceExecutor
from proteus.solver import ReplaySolver
from proteus.verifier import IndependentVerifier


def run_case(mutate: bool) -> bool:
    task = demo_task()
    with tempfile.TemporaryDirectory(prefix="proteus-eval-") as directory:
        root = Path(directory)
        evidence = EvidenceLedger(root / "evidence.jsonl")
        Compiler().compile(task, root)
        for action in ReplaySolver(demo_actions(task)).actions():
            WorkspaceExecutor(root, evidence).execute(action)
        if mutate:
            (root / task.deliverables[0].path).write_text("print('tampered')\n", encoding="utf-8")
        result = IndependentVerifier().verify(task, root)
        expected = not mutate
        print(f"{'known_bad' if mutate else 'baseline'}: passed={result.passed} expected={expected}")
        return result.passed == expected


def main() -> int:
    return 0 if run_case(False) and run_case(True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
