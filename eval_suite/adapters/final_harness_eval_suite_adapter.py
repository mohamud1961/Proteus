"""Adapter for final-harness task-pack rows into runnable execution specs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass(frozen=True)
class FinalSuiteRowSpec:
    row_id: str
    row_type: str
    is_flagship: bool
    provenance_type: str
    critical_clusters: tuple[str, ...]
    task_pack_ref: str | None
    task_pack_id: str
    canonical_workspace_root: str
    runtime_python_command: str
    max_solver_seconds: int
    surface_type: str
    legacy_layout: bool
    expected_candidate_output: str
    execution_source: str = "task_pack"
    lane_id: str = "private_board"
    benchmark_name: str | None = None
    benchmark_case_id: str | None = None
    benchmark_adapter: str | None = None
    difficulty_tier: str | None = None
    authority_label: str | None = None
    challenge_task_id: str | None = None
    adapter_supported: bool | None = None
    network_enabled: bool = False


def load_final_suite_row_specs(repo_root: Path) -> list[FinalSuiteRowSpec]:
    suite_dir = repo_root / "work/ledger/final_harness_eval_suite"
    final_registry = _load_yaml(suite_dir / "final_suite_registry.yaml")
    hard_registry = _load_yaml(suite_dir / "hard_task_registry.yaml")
    sentinel_registry = _load_yaml(suite_dir / "sentinel_composition_board.yaml")
    specs: list[FinalSuiteRowSpec] = []
    specs.extend(_load_private_board_specs(repo_root, final_registry, hard_registry, sentinel_registry))
    specs.extend(_load_official_benchmark_specs(suite_dir))
    specs.extend(_load_terminalbench_challenge_specs(suite_dir))
    return specs


def _load_private_board_specs(
    repo_root: Path,
    final_registry: dict[str, Any],
    hard_registry: dict[str, Any],
    sentinel_registry: dict[str, Any],
) -> list[FinalSuiteRowSpec]:
    row_to_pack_ref: dict[str, str] = {}
    for item in hard_registry.get("hard_tasks", []):
        if isinstance(item, dict) and isinstance(item.get("row_id"), str):
            slot = item.get("task_pack_slot", {})
            if isinstance(slot, dict) and isinstance(slot.get("task_pack_ref"), str):
                row_to_pack_ref[item["row_id"]] = slot["task_pack_ref"]
    for item in sentinel_registry.get("rows", []):
        if isinstance(item, dict) and isinstance(item.get("row_id"), str):
            slot = item.get("task_pack_slot", {})
            if isinstance(slot, dict) and isinstance(slot.get("task_pack_ref"), str):
                row_to_pack_ref[item["row_id"]] = slot["task_pack_ref"]

    ordered_rows: list[dict[str, Any]] = []
    ordered_rows.extend([item for item in final_registry.get("hard_rows", []) if isinstance(item, dict)])
    ordered_rows.extend([item for item in final_registry.get("sentinel_or_composition_rows", []) if isinstance(item, dict)])

    specs: list[FinalSuiteRowSpec] = []
    for row in ordered_rows:
        row_id = str(row["row_id"])
        task_pack_ref = row_to_pack_ref.get(row_id)
        if not task_pack_ref:
            raise ValueError(f"missing task pack ref for row {row_id}")
        task_pack_path = repo_root / task_pack_ref
        task_pack = _load_yaml(task_pack_path)
        specs.append(
            _normalize_private_row(
                row=row,
                task_pack_ref=task_pack_ref,
                task_pack=task_pack,
                fixture_manifest_path=task_pack_path.parent / "fixture_manifest.json",
            )
        )
    return specs


def _load_official_benchmark_specs(suite_dir: Path) -> list[FinalSuiteRowSpec]:
    manifest = _load_yaml(suite_dir / "official_benchmark_family_board.yaml")
    specs: list[FinalSuiteRowSpec] = []
    seen_benchmarks: set[str] = set()
    for family in manifest.get("benchmark_families", []):
        if not isinstance(family, dict):
            continue
        benchmark = str(family.get("benchmark", "")).strip()
        adapter_key = str(family.get("adapter_key", "")).strip()
        if not benchmark:
            continue
        if not adapter_key:
            raise ValueError(f"benchmark {benchmark} missing adapter_key")
        seen_benchmarks.add(benchmark.lower())
        selected_rows = [row for row in family.get("selected_rows", []) if isinstance(row, dict)]
        if len(selected_rows) > 3:
            raise ValueError(f"benchmark {benchmark} exceeds max 3 selected rows")
        for row in selected_rows:
            specs.append(
                FinalSuiteRowSpec(
                    row_id=str(row["row_id"]),
                    row_type=str(row.get("row_type", "official_benchmark")),
                    is_flagship=False,
                    provenance_type=str(row.get("provenance_type", "official_benchmark")),
                    critical_clusters=tuple(str(item) for item in row.get("critical_clusters", [])),
                    task_pack_ref=None,
                    task_pack_id=str(row.get("task_pack_id") or row["row_id"]),
                    canonical_workspace_root="/app",
                    runtime_python_command="python3",
                    max_solver_seconds=int(row.get("max_solver_seconds", 180)),
                    surface_type=str(row.get("surface_type", "terminal")),
                    legacy_layout=False,
                    expected_candidate_output="candidate",
                    execution_source="benchmark_adapter",
                    lane_id=str(manifest.get("lane_id", "official_benchmark_family_board_v1")),
                    benchmark_name=benchmark,
                    benchmark_case_id=str(row["benchmark_case_id"]),
                    benchmark_adapter=adapter_key,
                    difficulty_tier=str(row.get("difficulty_tier", "unknown")),
                    authority_label=str(row.get("authority_label", "equivalent")),
                    network_enabled=bool(row.get("network_enabled", False)),
                )
            )
    required = {"bfcl", "acebench", "contextbench", "letta"}
    missing = sorted(required - seen_benchmarks)
    if missing:
        raise ValueError(f"official benchmark board missing required benchmarks: {', '.join(missing)}")
    return specs


def _load_terminalbench_challenge_specs(suite_dir: Path) -> list[FinalSuiteRowSpec]:
    manifest = _load_yaml(suite_dir / "terminalbench_challenge_lane.yaml")
    specs: list[FinalSuiteRowSpec] = []
    for row in manifest.get("challenge_rows", []):
        if not isinstance(row, dict):
            continue
        specs.append(
            FinalSuiteRowSpec(
                row_id=str(row["row_id"]),
                row_type=str(row.get("row_type", "terminalbench_challenge")),
                is_flagship=bool(row.get("is_flagship", True)),
                provenance_type=str(row.get("provenance_type", "official_benchmark")),
                critical_clusters=tuple(str(item) for item in row.get("critical_clusters", [])),
                task_pack_ref=None,
                task_pack_id=str(row.get("task_pack_id") or row["row_id"]),
                canonical_workspace_root="/app",
                runtime_python_command="python3",
                max_solver_seconds=int(row.get("max_solver_seconds", 180)),
                surface_type=str(row.get("surface_type", "terminal")),
                legacy_layout=False,
                expected_candidate_output="candidate",
                execution_source="terminalbench_challenge",
                lane_id=str(manifest.get("lane_id", "terminalbench_challenge_lane_v1")),
                benchmark_name="TerminalBench",
                benchmark_case_id=str(row["task_id"]),
                benchmark_adapter="terminalbench",
                difficulty_tier=str(row.get("difficulty_tier", "challenge")),
                authority_label=str(row.get("authority_label", "equivalent")),
                challenge_task_id=str(row["task_id"]),
                adapter_supported=bool(row.get("adapter_supported", False)),
                network_enabled=bool(row.get("network_enabled", False)),
            )
        )
    return specs


def _normalize_private_row(
    *,
    row: dict[str, Any],
    task_pack_ref: str,
    task_pack: dict[str, Any],
    fixture_manifest_path: Path,
) -> FinalSuiteRowSpec:
    legacy_layout = task_pack.get("schema_version") != "final_harness_task_pack.v1"
    runtime_python = "python3"
    max_solver_seconds = 180
    canonical_workspace_root = "/app"
    expected_candidate_output = "candidate"
    surface_type = _surface_type_from_row_type(str(row.get("row_type", "")))

    if legacy_layout:
        fixture_manifest = _load_json(fixture_manifest_path)
        workspace_root = str(fixture_manifest.get("workspace_root") or "").strip("/")
        if workspace_root:
            canonical_workspace_root = f"/{workspace_root}"
        expected_candidate_output = "candidate"
    else:
        canonical_workspace_root = str(task_pack.get("canonical_workspace_root") or "/app")
        runtime_contract = task_pack.get("runtime_contract", {})
        if isinstance(runtime_contract, dict):
            runtime_python = str(runtime_contract.get("python_command") or runtime_python)
            max_solver_seconds = int(runtime_contract.get("max_solver_seconds") or max_solver_seconds)
        expected_outputs = task_pack.get("expected_outputs", {})
        if isinstance(expected_outputs, dict):
            candidate_output = expected_outputs.get("candidate_json")
            if isinstance(candidate_output, str) and candidate_output.strip():
                expected_candidate_output = candidate_output
        row_type = str(row.get("row_type", ""))
        if row_type == "sentinel":
            surface_type = "tool_call" if "tool-call" in " ".join(row.get("critical_clusters", [])) else "terminal"
        elif row_type == "composition":
            surface_type = "filesystem"
        else:
            surface_type = "terminal"

    return FinalSuiteRowSpec(
        row_id=str(row["row_id"]),
        row_type=str(row["row_type"]),
        is_flagship=bool(row.get("is_flagship", False)),
        provenance_type=str(row.get("provenance_type", "original_private")),
        critical_clusters=tuple(str(item) for item in row.get("critical_clusters", [])),
        task_pack_ref=task_pack_ref,
        task_pack_id=str(task_pack.get("task_pack_id") or row["row_id"]),
        canonical_workspace_root=canonical_workspace_root,
        runtime_python_command=runtime_python,
        max_solver_seconds=max_solver_seconds,
        surface_type=surface_type,
        legacy_layout=legacy_layout,
        expected_candidate_output=expected_candidate_output,
        execution_source="task_pack",
        lane_id="private_board",
        network_enabled=bool(row.get("network_enabled", False)),
    )


def _surface_type_from_row_type(row_type: str) -> str:
    if row_type == "sentinel":
        return "tool_call"
    if row_type == "composition":
        return "filesystem"
    return "terminal"


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is not None:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:  # pragma: no cover
        cmd = [
            "ruby",
            "-ryaml",
            "-rjson",
            "-e",
            "puts JSON.dump(YAML.load_file(ARGV[0]))",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return data


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data
