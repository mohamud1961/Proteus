#!/usr/bin/env python3
"""Verify an extracted exact-source package against its SHA-256 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    root = args.root.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {record["path"]: record for record in manifest["files"]}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if not (path.is_dir() and not path.is_symlink())
    }

    missing = sorted(set(expected) - actual)
    unexpected = sorted(actual - set(expected))
    mismatches: list[dict[str, str]] = []
    for relative, record in expected.items():
        path = root / relative
        if relative in missing:
            continue
        if path.is_symlink():
            digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
            kind = "symlink"
        elif path.is_file():
            digest = _sha256(path)
            kind = "file"
        else:
            digest = ""
            kind = "unsupported"
        actual_mode = f"{stat.S_IMODE(path.lstat().st_mode):04o}"
        expected_executable = record["git_mode"] == "100755"
        actual_executable = bool(stat.S_IMODE(path.lstat().st_mode) & 0o111)
        mode_matches = kind == "symlink" or expected_executable == actual_executable
        if (
            digest != record["sha256"]
            or kind != record["kind"]
            or not mode_matches
        ):
            mismatches.append(
                {
                    "path": relative,
                    "expected_kind": record["kind"],
                    "actual_kind": kind,
                    "expected_sha256": record["sha256"],
                    "actual_sha256": digest,
                    "expected_mode": record["mode"],
                    "actual_mode": actual_mode,
                    "expected_executable": str(expected_executable).lower(),
                    "actual_executable": str(actual_executable).lower(),
                }
            )

    status = "verified" if not (missing or unexpected or mismatches) else "mismatch"
    receipt = {
        "schema": "aether_exact_git_source_verification_v1",
        "status": status,
        "source_commit": manifest["source_commit"],
        "source_tree": manifest["source_tree"],
        "manifest_sha256": _sha256(manifest_path),
        "verified_file_count": len(expected) - len(missing) - len(mismatches),
        "missing": missing,
        "unexpected": unexpected,
        "mismatches": mismatches,
    }
    args.receipt.resolve().write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if status == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
