"""Build deterministic, leakage-safe historical replay cases.

The builder consumes artifacts already present in the repository.  It never
turns evaluator truth into role input: role_input, evaluator_only and
provenance are separate objects, and validation fails closed when evaluator
fields appear in role_input.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


BUILDER_VERSION = "historical_replay.v1"
EVALUATOR_ONLY_KEYS = frozenset({
    "official_reward", "reward", "grader_truth", "prior_verdict",
    "historical_model_response", "future_trace", "audit_classification",
    "expected_replay_outcome", "official_result", "grader_result",
})
_ROLE_FORBIDDEN_KEYS = EVALUATOR_ONLY_KEYS | frozenset({
    "solver_response", "verifier_response", "architect_response",
    "model_response", "future_steps", "trace_after_checkpoint",
})

_VERIFIER_ROLE_FIELDS = frozenset({
    "schema_version", "snapshot_id", "reason", "step", "task_contract",
    "stable_envmap", "dynamic_state", "open_obligations", "active_findings",
    "state_inspection_handles", "compiled_evidence_requirements",
    "inspection_evidence_ceilings", "evidence_requirements",
})


class ReplayBuildError(ValueError):
    """Raised when a replay artifact is malformed or leaks evaluator truth."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayBuildError(f"cannot read JSON artifact {path}: {exc}") from exc


def _walk_keys(value: Any, *, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            yield path + "." + key_text, key_text
            yield from _walk_keys(item, path=path + "." + key_text)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_keys(item, path=f"{path}[{index}]")


def _assert_role_input_safe(role_input: Any, evaluator_only: Mapping[str, Any]) -> None:
    forbidden = []
    for path, key in _walk_keys(role_input):
        if key in _ROLE_FORBIDDEN_KEYS:
            forbidden.append(path)
    if forbidden:
        raise ReplayBuildError("evaluator-only keys leaked into role_input: " + ", ".join(forbidden))
    evaluator_strings = {
        _canonical(item) for item in evaluator_only.values()
        if isinstance(item, (dict, list, tuple))
    }
    for path, key in _walk_keys(role_input):
        del path, key
    if evaluator_strings and _canonical(role_input) in evaluator_strings:
        raise ReplayBuildError("role_input duplicates an evaluator-only payload")


def _trace_payload(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, Mapping):
        raise ReplayBuildError(f"trace must be an object: {path}")
    trace = payload.get("trace", payload)
    if not isinstance(trace, Mapping):
        raise ReplayBuildError(f"trace field must be an object: {path}")
    return {"outer": dict(payload), "trace": dict(trace)}


def _architect_case(path: Path) -> dict[str, Any]:
    loaded = _trace_payload(path)
    outer, trace = loaded["outer"], loaded["trace"]
    task = str(outer.get("task", trace.get("task", path.stem.replace(".trace", ""))))
    request = trace.get("architect_request", {})
    if not isinstance(request, Mapping):
        request = {}
    safe_request = {
        key: copy.deepcopy(request[key])
        for key in ("task_prompt", "envmap", "capability_index", "objective_graph", "eval_index", "required_ir_fields")
        if key in request
    }
    role_input = {"task": task, "image": str(outer.get("image", "")), "architect_request": safe_request}
    evaluator_only = {
        "official_reward": outer.get("reward"),
        "prior_verdict": outer.get("status"),
        "historical_model_response": trace.get("architect_config"),
        "expected_replay_outcome": {"valid": bool(trace.get("architect_config"))},
    }
    _assert_role_input_safe(role_input, evaluator_only)
    return {
        "case_id": f"architect:{task}",
        "replay_type": "architect_only",
        "fidelity": "exact_input_reconstructed",
        "role_input": role_input,
        "evaluator_only": evaluator_only,
        "provenance": {"source": str(path), "sha256": _sha256_path(path), "task": task},
    }


def _solver_case(path: Path) -> dict[str, Any] | None:
    loaded = _trace_payload(path)
    outer, trace = loaded["outer"], loaded["trace"]
    steps = trace.get("steps", [])
    if not isinstance(steps, list):
        return None
    checkpoint = next((item for item in steps if isinstance(item, Mapping) and isinstance(item.get("context_seen"), Mapping)), None)
    if checkpoint is None:
        return None
    step = int(checkpoint.get("step", 0))
    task = str(outer.get("task", path.stem.replace(".trace", "")))
    role_input = {"task": task, "step": step, "context": copy.deepcopy(dict(checkpoint["context_seen"]))}
    evaluator_only = {
        "official_reward": outer.get("reward"),
        "prior_verdict": outer.get("status"),
        "historical_model_response": checkpoint.get("turn"),
        "future_trace": [item for item in steps if isinstance(item, Mapping) and int(item.get("step", -1)) > step],
        "expected_replay_outcome": {"status": outer.get("status"), "reward": outer.get("reward")},
    }
    _assert_role_input_safe(role_input, evaluator_only)
    return {
        "case_id": f"solver:{task}:step-{step}",
        "replay_type": "solver_pre_turn_checkpoint",
        "fidelity": "exact_pre_turn_context",
        "role_input": role_input,
        "evaluator_only": evaluator_only,
        "provenance": {"source": str(path), "sha256": _sha256_path(path), "task": task, "step": step},
    }


def _verifier_case(path: Path) -> dict[str, Any]:
    packet = _load_json(path)
    if not isinstance(packet, Mapping):
        raise ReplayBuildError(f"verifier packet must be an object: {path}")
    # Historical packet artifacts predate the neutral v2 contract and often
    # contain prompts, strategy, command receipts, or Solver journey fields.
    # Never replay those fields.  Project only the state-only contract and mark
    # the case reconstructed so exact-vs-reconstructed fidelity is explicit.
    raw_prompt = str(packet.get("task_prompt", "historical task state"))
    task_contract = packet.get("task_contract")
    if not isinstance(task_contract, Mapping):
        task_contract = {
            "raw_task_prompt": raw_prompt,
            "clauses": [{"clause_id": "historical:task", "text": raw_prompt, "exact_atoms": []}],
        }
    stable_envmap = packet.get("stable_envmap")
    if not isinstance(stable_envmap, Mapping):
        legacy_env = packet.get("envmap")
        if not isinstance(legacy_env, Mapping):
            legacy_env = {}
        stable_envmap = {
            "schema_version": "stable_envmap.v1",
            "version": 1,
            "sha256": _sha256_bytes(_canonical({"version": 1, "facts": dict(legacy_env)}).encode("utf-8")),
            "facts": copy.deepcopy(dict(legacy_env)),
        }
    dynamic_state = packet.get("dynamic_state")
    if not isinstance(dynamic_state, Mapping):
        dynamic_state = {
            "schema_version": "dynamic_world_state.v1",
            "state_version": int(packet.get("step", 0) or 0),
            "files": copy.deepcopy(packet.get("artifacts_present", {})) if isinstance(packet.get("artifacts_present"), Mapping) else {},
            "services": {},
            "jobs": {},
            "active_findings": copy.deepcopy(packet.get("active_findings", [])) if isinstance(packet.get("active_findings"), list) else [],
        }
    role_input = {
        "schema_version": "verifier_replay_input.v2",
        "snapshot_id": str(packet.get("snapshot_id", f"historical:{path.stem}")),
        "reason": str(packet.get("reason", "historical_replay")),
        "step": int(packet.get("step", 0) or 0),
        "task_contract": copy.deepcopy(dict(task_contract)),
        "stable_envmap": copy.deepcopy(dict(stable_envmap)),
        "dynamic_state": copy.deepcopy(dict(dynamic_state)),
        "open_obligations": copy.deepcopy(packet.get("open_obligations", [])),
        "active_findings": copy.deepcopy(packet.get("active_findings", [])),
        "state_inspection_handles": copy.deepcopy(packet.get("state_inspection_handles", [])),
        "compiled_evidence_requirements": copy.deepcopy(packet.get("compiled_evidence_requirements", [])),
        "inspection_evidence_ceilings": copy.deepcopy(packet.get("inspection_evidence_ceilings", {})),
        "evidence_requirements": copy.deepcopy(packet.get("evidence_requirements", {})),
    }
    assert set(role_input).issubset(_VERIFIER_ROLE_FIELDS | {"schema_version", "snapshot_id", "reason", "step"})
    evaluator_only = {
        "official_reward": None,
        "prior_verdict": None,
        "historical_model_response": None,
        "expected_replay_outcome": {"source_packet": str(path)},
    }
    _assert_role_input_safe(role_input, evaluator_only)
    return {
        "case_id": "verifier:" + path.parent.name + ":" + path.stem,
        "replay_type": "frozen_verifier_packet",
        "fidelity": "reconstructed_state_only",
        "role_input": role_input,
        "evaluator_only": evaluator_only,
        "provenance": {"source": str(path), "sha256": _sha256_path(path)},
    }


def _trace_paths(root: Path) -> list[Path]:
    return sorted(root.glob("phase2_traces/mini/*.trace.json"))


def _packet_paths(root: Path) -> list[Path]:
    candidates = sorted(
        path for path in root.rglob("verifier_packet.json")
        if "backup" not in path.parts and ".pytest_cache" not in path.parts
    )
    return candidates


def build_historical_manifest(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    traces = _trace_paths(root_path)
    if len(traces) < 8:
        raise ReplayBuildError(f"need at least 8 historical traces, found {len(traces)}")
    cases: list[dict[str, Any]] = []
    for path in traces[:8]:
        cases.append(_architect_case(path))
    solver_cases = [_solver_case(path) for path in traces]
    cases.extend(item for item in solver_cases if item is not None)  # filtered below
    solver_start = 8
    solver_end = solver_start + 6
    architect_cases = cases[:8]
    solver_cases_final = cases[solver_start:solver_end]
    packets = _packet_paths(root_path)
    if len(packets) < 9:
        raise ReplayBuildError(f"need at least 9 frozen verifier packets, found {len(packets)}")
    verifier_cases = [_verifier_case(path) for path in packets[:9]]
    all_cases = architect_cases + solver_cases_final + verifier_cases
    manifest = {
        "schema_version": "historical_replay_manifest.v1",
        "builder_version": BUILDER_VERSION,
        "counts": {
            "architect_only": len(architect_cases),
            "solver_pre_turn_checkpoints": len(solver_cases_final),
            "frozen_verifier_packets": len(verifier_cases),
            "total": len(all_cases),
        },
        "cases": all_cases,
    }
    validate_manifest(manifest)
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    return manifest


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return _sha256_bytes(_canonical(payload).encode("utf-8"))


def build_expectations(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return evaluator-only expectations kept out of role inputs."""
    validate_manifest(manifest)
    return {
        "schema_version": "historical_replay_expectations.v1",
        "cases": {
            str(case["case_id"]): {
                "replay_type": case["replay_type"],
                "fidelity": case["fidelity"],
                "expected_replay_outcome": copy.deepcopy(
                    case.get("evaluator_only", {}).get("expected_replay_outcome")
                ),
            }
            for case in manifest["cases"]
        },
    }


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != "historical_replay_manifest.v1":
        raise ReplayBuildError("unsupported replay manifest schema")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ReplayBuildError("manifest cases must be a non-empty list")
    ids: set[str] = set()
    counts = {"architect_only": 0, "solver_pre_turn_checkpoint": 0, "frozen_verifier_packet": 0}
    for case in cases:
        if not isinstance(case, Mapping):
            raise ReplayBuildError("replay case must be an object")
        case_id = str(case.get("case_id", ""))
        if not case_id or case_id in ids:
            raise ReplayBuildError(f"duplicate/empty replay case id: {case_id}")
        ids.add(case_id)
        role_input = case.get("role_input")
        evaluator_only = case.get("evaluator_only")
        if not isinstance(evaluator_only, Mapping):
            raise ReplayBuildError(f"{case_id}: evaluator_only must be an object")
        _assert_role_input_safe(role_input, evaluator_only)
        replay_type = str(case.get("replay_type", ""))
        if replay_type not in counts:
            raise ReplayBuildError(f"{case_id}: unsupported replay type {replay_type}")
        provenance = case.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ReplayBuildError(f"{case_id}: provenance must be an object")
        source = provenance.get("source")
        declared_hash = str(provenance.get("sha256", ""))
        if not source or len(declared_hash) != 64:
            raise ReplayBuildError(f"{case_id}: provenance hash is missing")
        source_path = Path(str(source))
        if not source_path.exists() or _sha256_path(source_path) != declared_hash:
            raise ReplayBuildError(f"{case_id}: provenance source hash mismatch")
        counts[replay_type] += 1
    declared = manifest.get("counts", {})
    expected = {
        "architect_only": counts["architect_only"],
        "solver_pre_turn_checkpoints": counts["solver_pre_turn_checkpoint"],
        "frozen_verifier_packets": counts["frozen_verifier_packet"],
        "total": len(cases),
    }
    if dict(declared) != expected:
        raise ReplayBuildError(f"manifest counts mismatch: declared={declared}, actual={expected}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expectations-out", type=Path)
    args = parser.parse_args(argv)
    manifest = build_historical_manifest(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.expectations_out:
        args.expectations_out.parent.mkdir(parents=True, exist_ok=True)
        args.expectations_out.write_text(
            json.dumps(build_expectations(manifest), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
