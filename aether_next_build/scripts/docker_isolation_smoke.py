#!/usr/bin/env python3
"""Docker mount-isolation + golden-grader smoke.

Proves, with real containers, across representative capability classes:
  1. The solver-phase container has NO /task and NO /tests (probed from
     inside the container by the solver itself; the probe output is a
     run_command receipt, not a harness claim).
  2. The official grader still scores after /task and /tests are introduced
     post-terminal (golden pass on a correct artifact).
  3. The grader is falsifiable: a known-bad artifact scores reward 0.0 even
     though the (stub) verifier claimed completion -- reconciled as
     verifier_false_clean at the record layer.

Uses scripted stub models only (offline; no credentials).  Writes evidence to
DOCKER_ISOLATION_SMOKE_<UTC>.json next to the repo's other run artifacts.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aether_next.runners.docker_runner import run_tbench_task  # noqa: E402
from aether_next.classifier import reconcile_grader_alignment  # noqa: E402

# The probe runs DURING the solver phase and records what it saw into the
# workspace; the grader (which runs only after /task and /tests are
# introduced) then verifies the recorded ABSENT markers.  reward=1.0 is
# therefore a joint proof of solver-phase isolation and grader function.
ISOLATION_PROBE = (
    "{ test -d /task && echo TASK_PRESENT || echo TASK_ABSENT; "
    "test -d /tests && echo TESTS_PRESENT || echo TESTS_ABSENT; } "
    "| tee /app/isolation_probe.txt"
)


def _config_json() -> str:
    return json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "isolation smoke task",
        "success_definition": "the requested artifact/service state exists",
        "solver_system_prompt": {
            "role": "smoke solver",
            "workflow": ["probe", "produce", "submit"],
            "self_verification": ["inspect state"],
            "memory_use": ["none"],
            "stop_conditions": ["after producing state"],
        },
        "verifier_system_prompt": {
            "role": "SMOKE-VERIFIER: read-only current-state verifier",
            "success_criteria": ["requested state present"],
            "required_evidence": ["current state"],
            "false_positive_traps": ["presence is not correctness"],
            "verdict_guidance": ["judge current state"],
            "feedback_guidance": ["concrete"],
        },
        "evidence_requirements": ["current artifact/service state"],
        "false_positive_risks": ["wrong content"],
        "minimum_completion_evidence": ["current state"],
        "tool_policy": {"enabled_tools": [
            "read_file", "write_file", "run_command",
            "launch_process", "probe_service", "run_check",
        ]},
        "context_policy": {"mode": "retrieval_augmented"},
        "model_verifier_policy": {"enabled": True},
    })


class ArchitectAndVerifierStub:
    """One callable serving both roles (ModelHooks reuses the architect
    callable for verification when no verifier model is given)."""

    def __call__(self, messages, *, max_output_tokens: int = 8000) -> str:
        system = messages[0]["content"] if messages else ""
        if "SMOKE-VERIFIER" in system:
            return json.dumps({
                "verdict": "completed",
                "confidence": "high",
                "summary": "stub verifier accepts current state (grader remains the authority)",
                "completion_evidence": ["stub_smoke_verifier"],
            })
        return _config_json()


class ScriptedSolver:
    def __init__(self, turns: list[dict]) -> None:
        self._turns = [json.dumps(t) for t in turns]

    def __call__(self, messages, *, max_output_tokens: int = 8000) -> str:
        if self._turns:
            return self._turns.pop(0)
        return json.dumps({"kind": "submit_outcome", "summary": "done"})


def _act(actions: list[dict], summary: str) -> dict:
    return {"kind": "act", "summary": summary, "actions": actions}


def _action(aid: str, kind: str, cap: str, args: dict, intent: str) -> dict:
    return {
        "action_id": aid, "kind": kind, "capability_id": cap, "arguments": args,
        "intent": intent, "expected_observation": "observed", "if_fail_next": "report blocker",
    }


def _write_task(root: Path, name: str, instruction: str, grader: str) -> Path:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text(instruction)
    (task / "task.toml").write_text(
        '[metadata]\ncategory = "smoke"\ndifficulty = "easy"\n'
        "[agent]\ntimeout_sec = 300.0\n[verifier]\ntimeout_sec = 300.0\n"
    )
    (task / "tests" / "test.sh").write_text(grader)
    return task


def run_case_file_class(work: Path) -> dict:
    task = _write_task(
        work, "smoke-file-golden",
        "Create /app/out.txt containing exactly 'hello-golden'.",
        "#!/bin/bash\nmkdir -p /logs/verifier\n"
        "if grep -q TASK_ABSENT /app/isolation_probe.txt "
        "&& grep -q TESTS_ABSENT /app/isolation_probe.txt "
        "&& ! grep -q TASK_PRESENT /app/isolation_probe.txt "
        "&& grep -q hello-golden /app/out.txt; then\n"
        "  echo 1 > /logs/verifier/reward.txt\nelse\n  echo 0 > /logs/verifier/reward.txt\nfi\n",
    )
    solver = ScriptedSolver([
        _act([
            _action("a-probe", "run_command", "shell", {"command": ISOLATION_PROBE}, "probe isolation"),
            _action("a-w", "write_file", "filesystem", {"path": "out.txt", "content": "hello-golden"}, "produce artifact"),
        ], "probe isolation then write artifact"),
    ])
    record = run_tbench_task(
        task_dir=str(task), image="debian:stable-slim",
        architect_model=ArchitectAndVerifierStub(), solver_model=solver,
        max_steps=4, run_timeout_s=600,
        run_provenance={"purpose": "docker_isolation_smoke", "case": "file_class_golden"},
    )
    return record


def run_case_file_known_bad(work: Path) -> dict:
    task = _write_task(
        work, "smoke-file-known-bad",
        "Create /app/out.txt containing exactly 'hello-golden'.",
        "#!/bin/bash\nmkdir -p /logs/verifier\n"
        "if grep -q hello-golden /app/out.txt; then echo 1 > /logs/verifier/reward.txt; "
        "else echo 0 > /logs/verifier/reward.txt; fi\n",
    )
    solver = ScriptedSolver([
        _act([
            _action("a-w", "write_file", "filesystem", {"path": "out.txt", "content": "wrong-content"}, "produce WRONG artifact"),
        ], "write known-bad artifact"),
    ])
    record = run_tbench_task(
        task_dir=str(task), image="debian:stable-slim",
        architect_model=ArchitectAndVerifierStub(), solver_model=solver,
        max_steps=4, run_timeout_s=600,
        run_provenance={"purpose": "docker_isolation_smoke", "case": "file_class_known_bad"},
    )
    return record


def run_case_service_class(work: Path) -> dict:
    task = _write_task(
        work, "smoke-service-golden",
        "Serve the current directory over HTTP on port 8000 inside the container.",
        "#!/bin/bash\nmkdir -p /logs/verifier\n"
        "ok=1\n"
        "grep -q TASK_ABSENT /app/isolation_probe.txt || ok=0\n"
        "grep -q TESTS_ABSENT /app/isolation_probe.txt || ok=0\n"
        "python3 - <<'PY' || ok=0\nimport urllib.request\nresp = urllib.request.urlopen('http://127.0.0.1:8000/', timeout=10)\nassert resp.status == 200, resp.status\nprint('service ok')\nPY\n"
        "echo $ok > /logs/verifier/reward.txt\n",
    )
    solver = ScriptedSolver([
        _act([
            _action("a-probe", "run_command", "shell", {"command": ISOLATION_PROBE}, "probe isolation"),
            _action("a-svc", "launch_process", "managed_process",
                    {"service_name": "httpd", "command": "python3 -m http.server 8000 --bind 127.0.0.1"},
                    "launch service"),
            _action("a-wait", "run_command", "shell",
                    {"command": "for i in $(seq 1 20); do python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/',timeout=2)\" && break || sleep 1; done; echo ready"},
                    "wait for service"),
        ], "probe isolation, launch http service, wait ready"),
    ])
    record = run_tbench_task(
        task_dir=str(task), image="python:3.11-slim",
        architect_model=ArchitectAndVerifierStub(), solver_model=solver,
        max_steps=4, run_timeout_s=900,
        run_provenance={"purpose": "docker_isolation_smoke", "case": "service_class_golden"},
    )
    return record


def main() -> int:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results: dict = {"stamp_utc": stamp, "cases": {}}
    with tempfile.TemporaryDirectory(prefix="aether_smoke_tasks_") as tmp:
        work = Path(tmp)
        for label, runner in (
            ("file_class_golden", run_case_file_class),
            ("file_class_known_bad", run_case_file_known_bad),
            ("service_class_golden", run_case_service_class),
        ):
            print(f"[smoke] running {label} ...", flush=True)
            record = runner(work)
            record["grader_alignment"] = reconcile_grader_alignment(
                reward=record.get("reward"),
                grader_error=record.get("grader_error"),
                kernel_status=str(record.get("status", "")),
                verifier_verdict="completed" if record.get("status") == "completed" else None,
            )
            results["cases"][label] = record
            print(f"[smoke] {label}: status={record.get('status')} reward={record.get('reward')} "
                  f"error={record.get('error')}", flush=True)

    out = ROOT / f"DOCKER_ISOLATION_SMOKE_{stamp}.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"[smoke] evidence written to {out}")

    # ---- Hard assertions (falsifiable) ------------------------------------
    golden = results["cases"]["file_class_golden"]
    bad = results["cases"]["file_class_known_bad"]
    service = results["cases"]["service_class_golden"]
    failures: list[str] = []
    if golden.get("reward") != 1.0:
        failures.append(f"golden file case reward={golden.get('reward')} (expected 1.0)")
    if bad.get("reward") != 0.0:
        failures.append(f"known-bad reward={bad.get('reward')} (expected 0.0)")
    if bad.get("grader_alignment", {}).get("verifier_alignment_status") != "verifier_false_clean":
        failures.append("known-bad case not reconciled as verifier_false_clean")
    if service.get("reward") != 1.0:
        failures.append(f"service case reward={service.get('reward')} (expected 1.0)")
    print("[smoke] assertion failures:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
