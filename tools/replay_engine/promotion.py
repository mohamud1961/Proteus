"""Fail-closed promotion gates for historical replay before full runs.

This module does not run a model or declare benchmark success.  It evaluates
the evidence bundle produced by deterministic replay stages and refuses the
unrestricted-run decision when any required stage is absent, unverifiable, or
marked reconstructed without an explicit fidelity acknowledgement.
"""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # support both ``python -m replay_engine.promotion`` and direct CLI use
    from .builder import manifest_sha256 as compute_manifest_sha256, validate_manifest
except ImportError:  # pragma: no cover - exercised by the documented script path
    from builder import manifest_sha256 as compute_manifest_sha256, validate_manifest


REQUIRED_GATES: tuple[str, ...] = (
    "architect_only_8",
    "compiler_workbench_board",
    "verifier_frozen_9",
    "solver_checkpoints_6",
    "first_divergence_replay",
    "short_sentinels",
)


@dataclass(frozen=True)
class PromotionGate:
    name: str
    status: str
    evidence: str = ""
    detail: str = ""


@dataclass(frozen=True)
class PromotionDecision:
    status: str
    gates: tuple[PromotionGate, ...]
    manifest_sha256: str

    @property
    def ready(self) -> bool:
        return self.status == "READY FOR UNRESTRICTED FULL RUNS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "manifest_sha256": self.manifest_sha256,
            "gates": [
                {"name": item.name, "status": item.status, "evidence": item.evidence, "detail": item.detail}
                for item in self.gates
            ],
        }


def first_divergence(expected: Sequence[Any], observed: Sequence[Any]) -> dict[str, Any] | None:
    """Return the first deterministic sequence divergence, or ``None``."""
    limit = min(len(expected), len(observed))
    for index in range(limit):
        if expected[index] != observed[index]:
            return {
                "index": index,
                "expected": expected[index],
                "observed": observed[index],
                "kind": "value_mismatch",
            }
    if len(expected) != len(observed):
        return {
            "index": limit,
            "expected": expected[limit] if index_in_range(limit, expected) else None,
            "observed": observed[limit] if index_in_range(limit, observed) else None,
            "kind": "length_mismatch",
        }
    return None


def index_in_range(index: int, values: Sequence[Any]) -> bool:
    return 0 <= index < len(values)


def evaluate_promotion(
    manifest: Mapping[str, Any],
    *,
    gate_evidence: Mapping[str, Any] | None = None,
) -> PromotionDecision:
    """Evaluate all mandatory replay gates without inventing missing proof.

    ``gate_evidence`` is intentionally evaluator-only and must be supplied by
    the corresponding deterministic/model-backed stage.  A truthy value is
    not enough: each stage must carry ``status='passed'`` and an evidence path.
    """
    try:
        counts = _validate_any_manifest(manifest)
    except Exception as exc:
        return _blocked_decision(manifest, f"manifest validation failed: {exc}")
    evidence = gate_evidence if isinstance(gate_evidence, Mapping) else {}
    gates: list[PromotionGate] = []
    count_expectations = {
        "architect_only_8": ("architect_only", 8),
        "verifier_frozen_9": ("frozen_verifier_packets", 9),
        "solver_checkpoints_6": ("solver_pre_turn_checkpoints", 6),
    }
    for name, (key, expected) in count_expectations.items():
        actual = int(counts.get(key, -1))
        if actual != expected:
            gates.append(PromotionGate(name, "blocked", detail=f"manifest count {key}={actual}; expected {expected}"))
            continue
        gates.append(_evidence_gate(name, evidence.get(name)))
    for name in ("compiler_workbench_board", "first_divergence_replay", "short_sentinels"):
        gates.append(_evidence_gate(name, evidence.get(name)))
    ready = all(item.status == "passed" for item in gates)
    return PromotionDecision(
        status="READY FOR UNRESTRICTED FULL RUNS" if ready else "NOT READY FOR UNRESTRICTED FULL RUNS",
        gates=tuple(gates),
        manifest_sha256=str(manifest.get("manifest_sha256", "")),
    )


def _validate_any_manifest(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate either the original local schema or source-complete V6.1."""
    schema = str(manifest.get("schema_version", ""))
    if schema == "historical_replay_manifest.v1":
        validate_manifest(manifest)
        declared = str(manifest.get("manifest_sha256", "")).strip()
        actual = compute_manifest_sha256(manifest)
        if not declared or declared != actual:
            raise ValueError(f"manifest SHA256 mismatch: declared={declared or '<missing>'}; actual={actual}")
        return manifest.get("counts", {}) if isinstance(manifest.get("counts"), Mapping) else {}
    if schema != "1.0":
        raise ValueError(f"unsupported replay manifest schema: {schema}")
    cases = manifest.get("cases")
    mode_counts = manifest.get("mode_counts")
    if not isinstance(cases, list) or not isinstance(mode_counts, Mapping):
        raise ValueError("source-complete manifest requires cases and mode_counts")
    expected_hash = str(manifest.get("manifest_sha256", "")).strip()
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    actual_hash = __import__("hashlib").sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if not expected_hash or expected_hash != actual_hash:
        raise ValueError(f"manifest SHA256 mismatch: declared={expected_hash or '<missing>'}; actual={actual_hash}")
    allowed_modes = {"architect_only", "solver_checkpoint", "verifier_packet"}
    actual_counts: dict[str, int] = {}
    for case in cases:
        if not isinstance(case, Mapping) or str(case.get("mode", "")) not in allowed_modes:
            raise ValueError("source-complete manifest contains an invalid replay case")
        role_input = case.get("role_input")
        serialized = json.dumps(role_input, sort_keys=True, ensure_ascii=False)
        for key in ("reward", "run_status", "official_result", "expected_task_judgement", "historical_reward", "historical_run_status", "historical_parsed_result", "raw_verifier_output", "future_steps"):
            if f'"{key}"' in serialized:
                raise ValueError(f"evaluator-only field leaked into role input: {key}")
        mode = str(case["mode"])
        actual_counts[mode] = actual_counts.get(mode, 0) + 1
    if dict(mode_counts) != actual_counts:
        raise ValueError(f"mode counts mismatch: declared={mode_counts}; actual={actual_counts}")
    return {
        "architect_only": actual_counts.get("architect_only", 0),
        "solver_pre_turn_checkpoints": actual_counts.get("solver_checkpoint", 0),
        "frozen_verifier_packets": actual_counts.get("verifier_packet", 0),
    }


def _blocked_decision(manifest: Mapping[str, Any], detail: str) -> PromotionDecision:
    return PromotionDecision(
        status="NOT READY FOR UNRESTRICTED FULL RUNS",
        gates=tuple(PromotionGate(name, "blocked", detail=detail) for name in REQUIRED_GATES),
        manifest_sha256=str(manifest.get("manifest_sha256", "")),
    )


def _evidence_gate(name: str, value: Any) -> PromotionGate:
    if not isinstance(value, Mapping) or str(value.get("status", "")) != "passed":
        return PromotionGate(name, "blocked", detail="required stage evidence is absent or not passed")
    evidence = str(value.get("evidence", "")).strip()
    if not evidence:
        return PromotionGate(name, "blocked", detail="passed stage is missing an evidence path")
    evidence_path = Path(evidence)
    if not evidence_path.is_file() or evidence_path.stat().st_size == 0:
        return PromotionGate(name, "blocked", evidence=evidence, detail="evidence path does not point to a non-empty artifact")
    return PromotionGate(name, "passed", evidence=evidence, detail=str(value.get("detail", "")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else {}
    decision = evaluate_promotion(manifest, gate_evidence=evidence)
    payload = json.dumps(decision.as_dict(), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if decision.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
