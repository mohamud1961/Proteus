"""Durable, content-addressed run evidence finalisation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable


_EVIDENCE_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache"})


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def directory_manifest(root: str | Path) -> dict[str, Any]:
    """Hash every ordinary file below *root* without following symlinks."""
    base = Path(root).resolve()
    rows: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames
            if name not in _EVIDENCE_SKIP_DIRS and not (current / name).is_symlink()
        ]
        for name in sorted(filenames):
            path = current / name
            rel = path.relative_to(base).as_posix()
            if path.is_symlink():
                rows.append({
                    "path": rel,
                    "kind": "symlink",
                    "target": os.readlink(path),
                })
                continue
            try:
                stat = path.stat()
                digest = sha256_file(path)
            except OSError as exc:
                rows.append({"path": rel, "kind": "error", "error": str(exc)})
                continue
            rows.append({
                "path": rel,
                "kind": "file",
                "bytes": stat.st_size,
                "mode": oct(stat.st_mode & 0o7777),
                "sha256": digest,
            })
    payload = {"root": str(base), "files": rows}
    payload["aggregate_sha256"] = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload["file_count"] = sum(1 for row in rows if row.get("kind") == "file")
    return payload


def write_manifest(root: str | Path, destination: str | Path) -> dict[str, Any]:
    manifest = directory_manifest(root)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def copy_snapshot(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Copy exact snapshot bytes and return a content manifest."""
    src = Path(source)
    dst = Path(destination)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)
    return write_manifest(dst, dst.parent / f"{dst.name}.manifest.json")


def executing_source_identity(start_path: str | Path) -> dict[str, Any]:
    """Derive source identity from the checkout executing this module."""
    start = Path(start_path).resolve()
    identity: dict[str, Any] = {"start_path": str(start)}
    try:
        top = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, errors="replace", timeout=15, check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", top, "rev-parse", "HEAD"],
            capture_output=True, text=True, errors="replace", timeout=15, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", top, "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, errors="replace", timeout=15, check=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", top, "branch", "--show-current"],
            capture_output=True, text=True, errors="replace", timeout=15, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", top, "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, errors="replace", timeout=30, check=True,
        ).stdout.splitlines()
        identity.update({
            "git_available": True,
            "git_top": top,
            "commit": head,
            "tree": tree,
            "branch": branch,
            "clean": not bool(status),
            "status": status,
        })
    except (OSError, subprocess.SubprocessError) as exc:
        identity.update({"git_available": False, "git_error": str(exc), "clean": False})
    source_root = start / "aether_next"
    if not source_root.is_dir():
        source_root = start
    identity["source_manifest"] = directory_manifest(source_root)
    return identity


def export_content_addressed_files(
    source_paths: Iterable[str | Path],
    destination: str | Path,
) -> dict[str, Any]:
    """Copy existing files by SHA-256 and return a lossless manifest."""
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in source_paths:
        source = Path(raw)
        if not source.is_file():
            continue
        digest = sha256_file(source)
        size = source.stat().st_size
        key = (digest, size)
        suffix = source.suffix or ".bin"
        target = dest / f"{digest}{suffix}"
        if key not in seen and not target.exists():
            shutil.copy2(source, target)
        seen.add(key)
        rows.append({
            "source_path": str(source),
            "stored_path": str(target),
            "sha256": digest,
            "bytes": size,
        })
    manifest = {
        "files": rows,
        "unique_content_count": len(seen),
        "file_count": len(rows),
    }
    manifest_path = dest / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    manifest["manifest_sha256"] = sha256_file(manifest_path)
    return manifest


def finalize_evidence_directory(
    evidence_dir: str | Path,
    *,
    required_paths: Iterable[str | Path],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Write the final marker only after every required artefact is present."""
    root = Path(evidence_dir)
    root.mkdir(parents=True, exist_ok=True)
    required_rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in required_paths:
        path = Path(raw)
        if not path.exists():
            missing.append(str(path))
            continue
        if path.is_file():
            required_rows.append({
                "path": str(path),
                "kind": "file",
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            })
        elif path.is_dir():
            manifest = directory_manifest(path)
            required_rows.append({
                "path": str(path),
                "kind": "directory",
                "aggregate_sha256": manifest["aggregate_sha256"],
                "file_count": manifest["file_count"],
            })
    if missing:
        raise RuntimeError("cannot finalise evidence; missing required paths: " + ", ".join(missing))
    marker_payload = {
        "status": "finalized",
        "required_evidence": required_rows,
        "metadata": metadata,
    }
    marker_payload["aggregate_sha256"] = hashlib.sha256(
        json.dumps(marker_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    marker = root / "FINALIZED.json"
    marker.write_text(json.dumps(marker_payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "path": str(marker),
        "sha256": sha256_file(marker),
        "aggregate_sha256": marker_payload["aggregate_sha256"],
        "status": "finalized",
    }
