"""Native ContextBench bridge via official upstream evaluator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from eval_suite.adapters.contracts import build_adapter_result_row, validate_benchmark_adapter_case
from eval_suite.schemas.eval_substrate_contracts import validate_result_row, validate_task_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_ROOT = REPO_ROOT / "research/sources/codebases/ContextBench"
DEFAULT_GOLD_PATH = UPSTREAM_ROOT / "data/contextbench_verified.parquet"
ADAPTER_FAMILY = "contextbench_native_adapter"
ADAPTER_LABEL = "ContextBench native adapter"
AUTHORITY_LABEL = "native"
AUTHORITY_DETAIL = "contextbench_official_evaluate_runtime_and_gold_trajectory_semantics"
CONTAMINATION_LABELS = ["clean", "public_benchmark_row", "mirrored_resource", "official_subset"]


def native_preflight(
    *,
    upstream_root: Path = UPSTREAM_ROOT,
    gold_path: Path = DEFAULT_GOLD_PATH,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    blockers: list[str] = []
    required_paths = {
        "evaluate_py": upstream_root / "contextbench/evaluate.py",
        "gold_parquet": gold_path,
    }
    missing_paths = [name for name, path in required_paths.items() if not path.exists()]
    if missing_paths:
        blockers.append("missing_official_contextbench_assets")
    probe = subprocess.run(
        [
            python_executable,
            "-c",
            "import pyarrow,datasets; from contextbench.extractors import available; print('ok' if available() else 'tree_sitter_missing')",
        ],
        env={**os.environ, "PYTHONPATH": str(upstream_root)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if probe.returncode != 0:
        blockers.append("missing_contextbench_runtime_dependencies")
    elif "tree_sitter_missing" in probe.stdout:
        blockers.append("missing_contextbench_tree_sitter_runtime")
    return {
        "native_runtime_available": not blockers,
        "blocker_codes": blockers,
        "missing_paths": missing_paths,
        "python_executable": python_executable,
        "upstream_root": str(upstream_root),
        "gold_path": str(gold_path),
        "python_probe_stdout": (probe.stdout or "").strip(),
        "python_probe_stderr": (probe.stderr or "").strip()[-2000:],
        "python_probe_exit_code": probe.returncode,
    }


def run_contextbench_native_static(
    output_root: Path,
    *,
    upstream_root: Path = UPSTREAM_ROOT,
    gold_path: Path = DEFAULT_GOLD_PATH,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    preflight = native_preflight(upstream_root=upstream_root, gold_path=gold_path, python_executable=python_executable)
    if not preflight["native_runtime_available"]:
        raise RuntimeError(f"contextbench native preflight blocked: {preflight['blocker_codes']}")
    output_root.mkdir(parents=True, exist_ok=True)
    pred_path = output_root / "gold_ceiling_pred.jsonl"
    results_path = output_root / "official_results.jsonl"
    _write_gold_ceiling_pred(pred_path, gold_path=gold_path, python_executable=python_executable)
    cp = subprocess.run(
        [
            python_executable,
            "-m",
            "contextbench.evaluate",
            "--gold",
            str(gold_path),
            "--pred",
            str(pred_path),
            "--out",
            str(results_path),
        ],
        env={**os.environ, "PYTHONPATH": str(upstream_root)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout)[-4000:])
    results = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not results:
        raise RuntimeError("contextbench official evaluator produced no rows")
    result = results[0]
    return {
        "pred_path": str(pred_path),
        "results_path": str(results_path),
        "result": result,
        "stdout_tail": cp.stdout[-2000:],
        "stderr_tail": cp.stderr[-4000:],
    }


def build_task_pack(task_pack_id: str) -> dict[str, Any]:
    return validate_task_pack(
        {
            "task_id": task_pack_id,
            "task_prompt": "Retrieve context using the official ContextBench gold/trajectory format and score with upstream evaluate.py.",
            "fixture": {"type": "contextbench_official_gold_eval", "workspace_ref": "/app/contextbench", "request_ref": "/app/contextbench/pred.jsonl"},
            "canonical_root": "/app",
            "backend_requirements": {"certified_default": "linux_container", "debug_backend": "debug_local_no_sandbox", "network": "enabled"},
            "visible_verifier": {"command": "python -m contextbench.evaluate --gold <gold> --pred /app/contextbench/pred.jsonl --out /app/contextbench/results.jsonl", "native_verifier_execution": True},
            "hidden_verifier": {"command_shape": "official_contextbench_evaluator", "checks_ref": f"hidden://contextbench/native/{DEFAULT_GOLD_PATH.name}", "leak_hidden_checks_to_prompt": False, "native_verifier_execution": True},
            "grader": {"type": "contextbench_official_eval", "score_range": [0, 1]},
            "contamination_policy": {"status": "clean", "source": "mirrored_contextbench_official_assets", "public_benchmark_row": True},
            "artifact_capture_policy": {"capture": ["environment_manifest", "artifact_bundle", "verifier", "grader", "trace"]},
            "admission_level": "certified",
            "surface_type": "retrieval",
            "benchmark_adapter_contract": {"adapter_label": ADAPTER_LABEL, "authority_label": AUTHORITY_LABEL, "authority_detail": AUTHORITY_DETAIL, "expected_answer_format": "json", "hidden_truth_ref": f"hidden://contextbench/native/{DEFAULT_GOLD_PATH.name}", "row_provenance_ref": f"provenance://contextbench/native/{DEFAULT_GOLD_PATH.name}", "source_schema_version": "contextbench_native.v1"},
        }
    )


def build_result_row(*, task_pack_id: str, grade: dict[str, Any], artifact_refs: list[str], trace_refs: list[str], verifier_ref: str, grader_ref: str) -> dict[str, Any]:
    row = build_adapter_result_row(
        run_id="contextbench-native-static-001",
        eval_id="contextbench-native-static",
        task_pack_id=task_pack_id,
        backend_ref="linux_container",
        environment_ref="certified://contextbench-native-static",
        verifier_ref=verifier_ref,
        grader_ref=grader_ref,
        benchmark_case=validate_benchmark_adapter_case(
            {
                "benchmark_family": ADAPTER_FAMILY,
                "benchmark_case_id": "contextbench_verified_gold_ceiling",
                "authority_label": AUTHORITY_LABEL,
                "surface_type": "retrieval",
                "admission_level": "certified",
                "expected_answer": {"format": "json", "value": {"hidden_truth_ref": "hidden://contextbench/native/gold_ceiling"}},
                "contamination_labels": list(CONTAMINATION_LABELS),
                "execution_unit": {"unit_id": f"{task_pack_id}::contextbench_verified_gold_ceiling", "task_prompt": "Official ContextBench evaluator run", "canonical_root": "/app", "execution_contract": {"authority_detail": AUTHORITY_DETAIL}},
            }
        ),
        native_grader_output=grade,
        trace_refs=trace_refs,
        artifact_refs=artifact_refs,
        failure_class="verification_grading",
    )
    row["adapter_label"] = ADAPTER_LABEL
    row["authority_detail"] = AUTHORITY_DETAIL
    return validate_result_row(row)


def _write_gold_ceiling_pred(path: Path, *, gold_path: Path, python_executable: str) -> None:
    code = """
import json, sys
import pyarrow.dataset as ds
table = ds.dataset(sys.argv[1], format='parquet').to_table(columns=['instance_id','repo_url','base_commit','gold_context'])
row = table.slice(0, 1).to_pylist()[0]
gold = json.loads(row['gold_context'])
pred_spans = {}
pred_files = []
for item in gold:
    file_path = str(item.get('file', '')).strip()
    if not file_path:
        continue
    pred_files.append(file_path)
    pred_spans.setdefault(file_path, []).append({'start': int(item.get('start_line', 1)), 'end': int(item.get('end_line', 1))})
payload = {
    'instance_id': row['instance_id'],
    'repo_url': row['repo_url'],
    'commit': row['base_commit'],
    'traj_data': {'pred_steps': [{'files': sorted(set(pred_files)), 'spans': pred_spans, 'symbols': {}}], 'pred_files': sorted(set(pred_files)), 'pred_spans': pred_spans, 'pred_symbols': {}},
    'model_patch': '',
}
with open(sys.argv[2], 'w', encoding='utf-8') as f:
    f.write(json.dumps(payload) + '\\n')
"""
    subprocess.run([python_executable, "-c", code, str(gold_path), str(path)], check=True, timeout=60)
