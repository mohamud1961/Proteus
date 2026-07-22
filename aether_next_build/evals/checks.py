"""Built-in harness checks that exercise production code without model calls."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable, Mapping


def _result(passed: bool, *, summary: str, metrics: Mapping[str, Any] | None = None,
            findings: list[str] | None = None) -> dict[str, Any]:
    return {
        "passed": bool(passed),
        "summary": summary,
        "metrics": dict(metrics or {}),
        "findings": list(findings or []),
    }


def architecture_purity(build_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Reject production imports of task-family or reference-only strategy code."""
    del manifest
    package = build_root / "aether_next"
    forbidden_import_fragments = ("task_capability", "reference_legacy")
    forbidden_runtime_files = {
        "video_processing.py", "qemu_strategy.py", "gcode_strategy.py",
        "crypto_strategy.py", "git_repair_strategy.py",
    }
    findings: list[str] = []
    parsed = 0
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(package).as_posix()
        if "/reference_legacy/" in f"/{rel}/":
            continue
        parsed += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append(f"cannot parse production module {rel}: {exc}")
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
            for name in names:
                if any(fragment in name for fragment in forbidden_import_fragments):
                    findings.append(f"production import {rel}: {name}")
        if path.name in forbidden_runtime_files:
            findings.append(f"task-family strategy module exists on production path: {rel}")

    required_files = (
        "kernel.py", "context_compiler.py", "ledger.py", "inspection_registry.py",
        "proof_contract.py", "kernel_verifier.py", "runners/docker_runner.py",
        "evidence_finalization.py", "network_policy.py", "observation_batch.py",
    )
    missing = [item for item in required_files if not (package / item).is_file()]
    findings.extend(f"missing canonical production owner: {item}" for item in missing)
    passed = not findings
    return _result(
        passed,
        summary=(
            "production path is free of task-family/reference imports"
            if passed else "architectural purity violations found"
        ),
        metrics={"production_modules_parsed": parsed, "required_owner_count": len(required_files)},
        findings=findings,
    )


def official_task_board_integrity(build_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    del manifest
    path = build_root / "evals" / "official_task_board.v1.json"
    findings: list[str] = []
    try:
        board = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(False, summary="official task board unreadable", findings=[str(exc)])

    full = board.get("full_board", [])
    smoke = board.get("smoke_board", [])
    task_count = int(board.get("source", {}).get("task_count", 0) or 0)
    if task_count != 90:
        findings.append(f"expected 90 official tasks, board declares {task_count}")
    if len(full) != task_count or len(set(full)) != len(full):
        findings.append(f"full board is not a unique {task_count}-task set")
    smoke_ids = [str(item.get("task_id", "")) for item in smoke if isinstance(item, dict)]
    if len(smoke_ids) != 24 or len(set(smoke_ids)) != 24:
        findings.append("smoke board must contain 24 unique tasks")
    unknown = sorted(set(smoke_ids) - set(full))
    if unknown:
        findings.append("smoke board contains unknown tasks: " + ", ".join(unknown))

    summary = board.get("corpus_summary", {})
    dimensions = set(summary.get("task_dimensions", []))
    surfaces = set(summary.get("verification_surfaces", []))
    risks = set(summary.get("harness_risks", []))
    smoke_coverage = {
        str(tag)
        for item in smoke if isinstance(item, dict)
        for tag in item.get("covers", [])
    }
    uncovered_dimensions = sorted(dimensions - smoke_coverage)
    uncovered_surfaces = sorted(surfaces - smoke_coverage)
    uncovered_risks = sorted(risks - smoke_coverage)
    if uncovered_dimensions:
        findings.append("smoke board misses task dimensions: " + ", ".join(uncovered_dimensions))
    # Surfaces and risks are allowed to be exercised partly by deterministic
    # synthetic cases, but the report must expose any board-only gaps.
    passed = not findings
    return _result(
        passed,
        summary="official task board is internally coherent" if passed else "official task board gaps found",
        metrics={
            "full_task_count": len(full),
            "smoke_task_count": len(smoke_ids),
            "task_dimension_count": len(dimensions),
            "verification_surface_count": len(surfaces),
            "harness_risk_count": len(risks),
            "smoke_uncovered_verification_surfaces": uncovered_surfaces,
            "smoke_uncovered_harness_risks": uncovered_risks,
        },
        findings=findings,
    )


def scorecard_eval_coverage(build_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    del build_root
    declared = {str(item) for item in manifest.get("scorecard_ids", [])}
    covered: dict[str, list[str]] = {item: [] for item in declared}
    findings: list[str] = []
    case_ids: set[str] = set()
    for case in manifest.get("cases", []):
        case_id = str(case.get("id", "")).strip()
        if not case_id:
            findings.append("case without id")
            continue
        if case_id in case_ids:
            findings.append(f"duplicate case id: {case_id}")
        case_ids.add(case_id)
        for scorecard_id in case.get("covers", []):
            scorecard_id = str(scorecard_id)
            if scorecard_id not in declared:
                findings.append(f"case {case_id} covers undeclared scorecard id {scorecard_id}")
            else:
                covered[scorecard_id].append(case_id)
    missing = sorted(item for item, owners in covered.items() if not owners)
    if missing:
        findings.append("scorecard ids without eval ownership: " + ", ".join(missing))
    passed = not findings
    return _result(
        passed,
        summary="every frozen scorecard item has eval ownership" if passed else "scorecard coverage incomplete",
        metrics={"scorecard_id_count": len(declared), "case_count": len(case_ids), "coverage": covered},
        findings=findings,
    )


def deterministic_system_scenarios(build_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    del build_root, manifest
    from aether_next.integration_scenarios import run_all_integration_scenarios

    rows = [item.as_dict() for item in run_all_integration_scenarios()]
    findings: list[str] = []
    for row in rows:
        failed_checks = sorted(key for key, value in row.get("checks", {}).items() if value is not True)
        if row.get("status") != "completed":
            findings.append(f"{row.get('scenario_id')}: status={row.get('status')}")
        if failed_checks:
            findings.append(f"{row.get('scenario_id')}: failed checks={failed_checks}")
    return _result(
        not findings,
        summary="all production-path deterministic scenarios passed" if not findings else "system scenario failure",
        metrics={"scenario_count": len(rows), "scenarios": rows},
        findings=findings,
    )


def context_growth_probe(build_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    del build_root, manifest
    from aether_next.compiler import CapabilityRegistry, ConfigCompiler
    from aether_next.context_compiler import ContextCompiler
    from aether_next.ledger import ExecutionLedger, Receipt
    from aether_next.runtime_ir import (
        CapabilityDescriptor, ContextPolicy, EnvMap, RuntimeConfigIR, stable_json,
    )

    env = EnvMap(
        task_prompt="Create a bounded output while retaining complete evidence.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "files", tool_names=("read_file", "write_file")),
        },
    )
    ir = RuntimeConfigIR(
        architect_summary="bounded context probe",
        solver_identity_prompt="Use current evidence.",
        selected_capabilities=("shell", "filesystem"),
        context_policy=ContextPolicy(mode="default_bounded", max_recent_receipts=8),
    )
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
    compiler = ContextCompiler()

    def packet_size(receipt_count: int) -> tuple[int, dict[str, Any]]:
        ledger = ExecutionLedger()
        for index in range(receipt_count):
            body = f"result-{index}:" + ("x" * 8192)
            ledger.record(Receipt(
                receipt_id=f"cmd:{index}",
                step=index,
                kind="run_command",
                success=True,
                summary=f"command {index}",
                payload={
                    "command": f"probe-{index}",
                    "stdout": body,
                    "stdout_full": body,
                    "stdout_handle": f"output:{index}:stdout",
                    "stdout_bytes": len(body),
                    "model_requested_action": True,
                    "solver_action_id": f"action-{index}",
                    "solver_action_kind": "run_command",
                },
            ))
        packet = compiler.compile(compiled, ledger, [])
        return len(stable_json(packet).encode("utf-8")), packet

    small_bytes, small = packet_size(32)
    large_bytes, large = packet_size(640)
    growth_ratio = round(large_bytes / max(1, small_bytes), 3)
    handles = large.get("output_handles", [])
    command_rows = large.get("command_results", [])
    findings: list[str] = []
    if large_bytes > 196_608:
        findings.append(f"640-receipt context is {large_bytes} bytes, above 196608-byte ceiling")
    if growth_ratio > 1.35:
        findings.append(f"context grew {growth_ratio}x from 32 to 640 receipts")
    if len(handles) > 16:
        findings.append(f"output handle section is unbounded: {len(handles)} rows")
    if len(command_rows) > 8:
        findings.append(f"command result section exceeds configured recent bound: {len(command_rows)}")
    return _result(
        not findings,
        summary="long-run context remains bounded and evidence remains queryable" if not findings else "context growth invariant failed",
        metrics={
            "packet_bytes_at_32_receipts": small_bytes,
            "packet_bytes_at_640_receipts": large_bytes,
            "growth_ratio": growth_ratio,
            "large_packet_output_handles": len(handles),
            "large_packet_command_results": len(command_rows),
            "large_packet_sections": sorted(large),
        },
        findings=findings,
    )


def archetype_matrix_coverage(build_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Require two-channel coverage for every official-task archetype.

    Each dimension/surface/risk needs public official-task representatives and
    deterministic test targets. The matrix itself remains eval-only.
    """
    board_path = build_root / "evals" / "official_task_board.v1.json"
    matrix_path = build_root / "evals" / "archetype_matrix.v1.json"
    try:
        board = json.loads(board_path.read_text(encoding="utf-8"))
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(False, summary="archetype matrix unreadable", findings=[str(exc)])

    official_tasks = set(board.get("full_board", []))
    expected = {
        "dimensions": set(board.get("corpus_summary", {}).get("task_dimensions", [])),
        "verification_surfaces": set(board.get("corpus_summary", {}).get("verification_surfaces", [])),
        "harness_risks": set(board.get("corpus_summary", {}).get("harness_risks", [])),
    }
    findings: list[str] = []
    target_count = 0
    official_reference_count = 0
    manifest_targets = {
        str(target)
        for case in manifest.get("cases", [])
        for target in case.get("targets", []) or []
    }
    full_suite_present = any(
        str(case.get("id", "")) == "canonical_current_suite"
        and "tests" in {str(target) for target in case.get("targets", []) or []}
        for case in manifest.get("cases", [])
    )

    for section, expected_keys in expected.items():
        rows = matrix.get(section, {})
        if set(rows) != expected_keys:
            missing = sorted(expected_keys - set(rows))
            extra = sorted(set(rows) - expected_keys)
            if missing:
                findings.append(f"{section} missing matrix rows: {', '.join(missing)}")
            if extra:
                findings.append(f"{section} has unknown matrix rows: {', '.join(extra)}")
        for name, row in rows.items():
            targets = [str(item) for item in row.get("deterministic_targets", [])]
            tasks = [str(item) for item in row.get("official_tasks", [])]
            if not targets:
                findings.append(f"{section}.{name} has no deterministic targets")
            if not tasks:
                findings.append(f"{section}.{name} has no official tasks")
            unknown_tasks = sorted(set(tasks) - official_tasks)
            if unknown_tasks:
                findings.append(f"{section}.{name} references unknown tasks: {', '.join(unknown_tasks)}")
            for target in targets:
                target_count += 1
                if not (build_root / target).is_file():
                    findings.append(f"{section}.{name} missing test target: {target}")
                elif target not in manifest_targets and not full_suite_present:
                    findings.append(f"{section}.{name} target is never executed by manifest: {target}")
            official_reference_count += len(tasks)

    return _result(
        not findings,
        summary=(
            "all official-task archetypes have deterministic and official-board coverage"
            if not findings else "archetype coverage matrix is incomplete"
        ),
        metrics={
            "dimension_count": len(expected["dimensions"]),
            "verification_surface_count": len(expected["verification_surfaces"]),
            "harness_risk_count": len(expected["harness_risks"]),
            "deterministic_target_references": target_count,
            "official_task_references": official_reference_count,
        },
        findings=findings,
    )


BUILTINS: dict[str, Callable[[Path, Mapping[str, Any]], dict[str, Any]]] = {
    "architecture_purity": architecture_purity,
    "official_task_board_integrity": official_task_board_integrity,
    "scorecard_eval_coverage": scorecard_eval_coverage,
    "deterministic_system_scenarios": deterministic_system_scenarios,
    "context_growth_probe": context_growth_probe,
    "archetype_matrix_coverage": archetype_matrix_coverage,
}
