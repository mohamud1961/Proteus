"""Source-complete V6.1 historical replay manifest builder.

This is the canonical replay-only path for the two supplied historical
archives. It never executes a model or mutates the archives. Evaluator truth
stays in ``evaluator_only`` and provenance carries archive/trace/packet hashes.
"""
from __future__ import annotations

import hashlib
import json
import argparse
from pathlib import Path
import tempfile
import zipfile
from typing import Any, Iterable


FORBIDDEN_ROLE_KEYS = {
    "reward", "run_status", "official_result", "expected_task_judgement",
    "allowed_execution_status", "required_owner", "allowed_failed_owner",
    "historical_failure", "historical_note", "original_verdict",
    "parsed_verifier_result", "raw_verifier_output", "future_steps",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_no_evaluator_leak(role_input: Any) -> None:
    serialized = canonical_json(role_input)
    for key in FORBIDDEN_ROLE_KEYS:
        if f'"{key}"' in serialized:
            raise ValueError(f"evaluator-only field leaked into role input: {key}")


def _sections(messages: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.startswith("["):
            continue
        first, sep, rest = content.partition("]\n")
        if sep:
            result[first[1:]] = rest
    return result


def _extract(zip_path: Path, destination: Path) -> Path:
    root = destination / zip_path.stem
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.filename.startswith("__MACOSX/") or member.filename.endswith("/.DS_Store"):
                continue
            archive.extract(member, root)
    return root


def _trace(root: Path, task: str) -> Path:
    path = root / "traces" / f"{task}.trace.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _verifier_dir(root: Path, task: str, checkpoint: str) -> Path:
    path = root / "verifier_evidence" / task / checkpoint
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _architect(root: Path, task: str, risks: list[str], archive: Path) -> dict[str, Any]:
    path = _trace(root, task)
    trace = _load(path)
    sections = _sections(trace.get("prefix_messages", []))
    role_input = {
        "task_prompt": sections.get("task_prompt", ""),
        "stable_envmap": sections.get("envmap", ""),
        "envmap_file_tree": sections.get("envmap_file_tree", ""),
        "fixed_action_schema": sections.get("action_schema", ""),
        "architect_contract": {
            "tools_are_kernel_owned": True,
            "must_emit_typed_config": True,
            "must_cover_every_task_clause": True,
            "must_author_solver_and_verifier_prompts": True,
        },
    }
    if not role_input["task_prompt"] or not role_input["stable_envmap"]:
        raise ValueError(f"architect replay input incomplete for {task}")
    _assert_no_evaluator_leak(role_input)
    return {
        "case_id": f"architect::{task}", "mode": "architect_only",
        "role_input": role_input,
        "evaluator_only": {
            "task": task, "historical_architect_config": trace.get("architect_config", {}),
            "historical_run_status": trace.get("status"), "historical_reward": trace.get("reward"),
            "task_specific_risks": risks,
            "score_dimensions": [
                "task_clause_coverage", "config_schema_validity", "config_realisability",
                "solver_prompt_strength", "verifier_prompt_strength", "process_mode_correctness",
                "context_policy_correctness", "evidence_and_falsification_quality",
                "recovery_and_reconfiguration_quality", "absence_of_tool_selection",
            ],
        },
        "provenance": {"archive": archive.name, "archive_sha256": sha256_file(archive),
                       "trace_path": str(path.relative_to(root)), "trace_sha256": sha256_file(path)},
    }


def _solver(root: Path, task: str, step_index: int, purpose: str, archive: Path) -> dict[str, Any]:
    path = _trace(root, task)
    trace = _load(path)
    matching = [item for item in trace.get("steps", []) if item.get("step") == step_index]
    if len(matching) != 1:
        raise ValueError(f"expected one step {step_index} for {task}, got {len(matching)}")
    step = matching[0]
    role_input = {
        "prefix_messages": trace.get("prefix_messages", []),
        "context_seen": step.get("context_seen", {}),
        "replay_contract": {"this_is_pre_turn_state": True, "no_future_steps_are_available": True,
                            "tools_may_be_disabled_for_reasoning_only_replay": True},
    }
    _assert_no_evaluator_leak(role_input)
    return {
        "case_id": f"solver::{task}::step_{step_index:04d}", "mode": "solver_checkpoint",
        "role_input": role_input,
        "evaluator_only": {"task": task, "step": step_index, "purpose": purpose,
                           "historical_turn": step.get("turn", {}), "historical_observations": step.get("observations", []),
                           "historical_run_status": trace.get("status"), "historical_reward": trace.get("reward")},
        "provenance": {"archive": archive.name, "archive_sha256": sha256_file(archive),
                       "trace_path": str(path.relative_to(root)), "trace_sha256": sha256_file(path), "step": step_index},
    }


def _verifier(root: Path, spec: dict[str, Any], archive: Path) -> dict[str, Any]:
    directory = _verifier_dir(root, spec["task"], spec["checkpoint"])
    packet_path = directory / "verifier_packet.json"
    prompt_path = directory / "verifier_prompt.txt"
    parsed_path = directory / "parsed_verifier_result.json"
    raw_path = directory / "raw_verifier_output.txt"
    trace = _load(_trace(root, spec["task"]))
    role_input = {
        "verifier_prompt": prompt_path.read_text(encoding="utf-8"),
        "frozen_verifier_packet": _load(packet_path),
        "replay_contract": {"judge_current_state_only": True, "do_not_score_solver_journey": True,
                            "do_not_assume_prior_verdict_is_correct": True},
    }
    _assert_no_evaluator_leak(role_input)
    evaluator = dict(spec)
    evaluator.update({"historical_parsed_result": _load(parsed_path) if parsed_path.exists() else None,
                      "historical_raw_output_sha256": sha256_file(raw_path) if raw_path.exists() else None,
                      "historical_run_status": trace.get("status"), "historical_reward": trace.get("reward")})
    return {"case_id": f"verifier::{spec['task']}::{spec['checkpoint']}::{archive.name}", "mode": "verifier_packet",
            "role_input": role_input, "evaluator_only": evaluator,
            "provenance": {"archive": archive.name, "archive_sha256": sha256_file(archive),
                           "packet_path": str(packet_path.relative_to(root)), "packet_sha256": sha256_file(packet_path),
                           "prompt_path": str(prompt_path.relative_to(root)), "prompt_sha256": sha256_file(prompt_path)}}


def build_source_complete_manifest(archive_paths: Iterable[str | Path], expectations_path: str | Path) -> dict[str, Any]:
    archives = [Path(item).resolve() for item in archive_paths]
    expectations = _load(Path(expectations_path))
    by_name = {path.name: path for path in archives}
    for name, expected in expectations["archive_sha256"].items():
        if name not in by_name or sha256_file(by_name[name]) != expected:
            raise ValueError(f"archive hash mismatch for {name}")
    with tempfile.TemporaryDirectory(prefix="aether-v61-replay-") as tmp:
        roots = {path.name: _extract(path, Path(tmp)) for path in archives}
        cases: list[dict[str, Any]] = []
        for task, archive_name in expectations["architect_latest_source"].items():
            cases.append(_architect(roots[archive_name], task, expectations["architect_task_risks"].get(task, []), by_name[archive_name]))
        for spec in expectations["solver_selected_checkpoints"]:
            cases.append(_solver(roots[spec["archive"]], spec["task"], spec["step"], spec["purpose"], by_name[spec["archive"]]))
        for spec in expectations["verifier_cases"]:
            cases.append(_verifier(roots[spec["archive"]], spec, by_name[spec["archive"]]))
    for case in cases:
        _assert_no_evaluator_leak(case["role_input"])
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["mode"]] = counts.get(case["mode"], 0) + 1
    manifest = {"schema_version": "1.0", "doctrine": {
        "role_input_never_contains_evaluator_truth": True,
        "no_future_trace_steps_in_solver_input": True,
        "prior_model_outputs_are_evaluator_only": True,
        "official_grader_truth_is_evaluator_only": True,
        "replay_does_not_modify_original_archives": True,
    }, "mode_counts": counts, "cases": cases}
    manifest["manifest_sha256"] = sha256_bytes(canonical_json(manifest).encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="append", required=True, type=Path)
    parser.add_argument("--expectations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = build_source_complete_manifest(args.archive, args.expectations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"mode_counts": manifest["mode_counts"], "manifest_sha256": manifest["manifest_sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
