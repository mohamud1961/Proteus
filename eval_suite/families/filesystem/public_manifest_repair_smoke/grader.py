from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def normalize_manifest(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def compute_checksum(normalized_manifest: str) -> str:
    return hashlib.sha256(normalized_manifest.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").rstrip("\n")


def grade_workspace(*, workspace_root: Path, reference_root: Path, mode: str = "visible") -> dict[str, Any]:
    candidate_manifest = _read_json(workspace_root / "release" / "manifest.json")
    candidate_summary = _read_text(workspace_root / "release" / "summary.txt")
    candidate_checksum = _read_text(workspace_root / "release" / "checksum.txt")

    reference_manifest = _read_json(reference_root / "manifest.json")
    reference_summary = _read_text(reference_root / "summary.txt")
    reference_checksum = _read_text(reference_root / "checksum.txt")

    reason_codes: list[str] = []
    checks = {
        "manifest_present": candidate_manifest is not None,
        "summary_present": candidate_summary is not None,
        "checksum_present": candidate_checksum is not None,
        "reference_manifest_present": reference_manifest is not None,
        "reference_summary_present": reference_summary is not None,
        "reference_checksum_present": reference_checksum is not None,
    }

    observed_normalized = normalize_manifest(candidate_manifest) if candidate_manifest is not None else None
    reference_normalized = normalize_manifest(reference_manifest) if reference_manifest is not None else None
    observed_checksum = candidate_checksum
    derived_checksum = compute_checksum(observed_normalized) if observed_normalized is not None else None

    if candidate_manifest is None:
        reason_codes.append("missing_release_manifest")
    if candidate_summary is None:
        reason_codes.append("missing_release_summary")
    if candidate_checksum is None:
        reason_codes.append("missing_release_checksum")
    if reference_manifest is None:
        reason_codes.append("missing_reference_manifest")
    if reference_summary is None:
        reason_codes.append("missing_reference_summary")
    if reference_checksum is None:
        reason_codes.append("missing_reference_checksum")

    if candidate_manifest is not None and reference_manifest is not None and observed_normalized != reference_normalized:
        reason_codes.append("manifest_mismatch")
    if candidate_summary is not None and reference_summary is not None and candidate_summary != reference_summary:
        reason_codes.append("summary_mismatch")
    if candidate_checksum is not None and reference_checksum is not None and candidate_checksum != reference_checksum:
        reason_codes.append("checksum_file_mismatch")
    if candidate_checksum is not None and derived_checksum is not None and candidate_checksum != derived_checksum:
        reason_codes.append("checksum_not_derived_from_manifest")

    verdict = "pass" if not reason_codes else "fail"
    score = 1.0 if verdict == "pass" else 0.0
    return {
        "mode": mode,
        "workspace_root": str(workspace_root),
        "reference_root": str(reference_root),
        "verdict": verdict,
        "score": score,
        "reason_codes": sorted(set(reason_codes)),
        "checks": checks,
        "observed_manifest_normalized": observed_normalized,
        "reference_manifest_normalized": reference_normalized,
        "observed_checksum": observed_checksum,
        "derived_checksum": derived_checksum,
        "reference_checksum": reference_checksum,
        "observed_summary": candidate_summary,
        "reference_summary": reference_summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade the public manifest repair smoke workspace.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--reference-root", required=True)
    parser.add_argument("--mode", choices=("visible", "hidden"), default="visible")
    args = parser.parse_args(argv)

    result = grade_workspace(
        workspace_root=Path(args.workspace),
        reference_root=Path(args.reference_root),
        mode=args.mode,
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
