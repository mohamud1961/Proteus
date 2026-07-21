#!/usr/bin/env python3
"""Build and verify a complete source package from an exact Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
from typing import Any


PROHIBITED_PARTS = {".DS_Store", "__pycache__"}
PROHIBITED_SUFFIXES = {".pyc", ".pyo"}
SENSITIVE_BASENAMES = {
    ".env",
    "azureProfile.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "model.env",
    "secrets.json",
}
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
PROVIDER_CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?m)^\s*(?:export\s+)?(?:AZURE_OPENAI_API_KEY|OPENAI_API_KEY)\s*=\s*\S+"
)


def _run(repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        args,
        cwd=repo,
        check=True,
        capture_output=True,
        text=text,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_entries(
    repo: Path, commit: str
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    result = _run(
        repo,
        "git",
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
        text=False,
    )
    entries: dict[str, dict[str, str]] = {}
    gitlinks: list[dict[str, str]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
        path = raw_path.decode("utf-8", errors="strict")
        if object_type == "blob":
            entries[path] = {"git_mode": mode, "git_object": object_id}
        elif object_type == "commit" and mode == "160000":
            gitlinks.append({"path": path, "git_mode": mode, "git_object": object_id})
        else:
            raise RuntimeError(f"unsupported Git entry type {object_type!r}: {path}")
    return entries, gitlinks


def _prohibited_paths(paths: set[str]) -> list[str]:
    findings: list[str] = []
    for path in sorted(paths):
        parts = Path(path).parts
        if any(part.startswith("._") or part in PROHIBITED_PARTS for part in parts):
            findings.append(path)
        elif Path(path).suffix in PROHIBITED_SUFFIXES:
            findings.append(path)
    return findings


def _sensitive_paths(paths: set[str]) -> list[str]:
    return sorted(
        path
        for path in paths
        if Path(path).name in SENSITIVE_BASENAMES
        or any(part in {".azure", ".ssh"} for part in Path(path).parts)
    )


def _content_audit(root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    private_key_candidates: list[str] = []
    provider_credential_candidates: list[str] = []
    for record in records:
        if record["kind"] != "file":
            continue
        path = root / record["path"]
        data = path.read_bytes()
        # Match material, not the scanner's own string constants.  A PEM header
        # is credential material only when it begins a content line; the source
        # code represents these headers inside quoted byte literals.
        if any(
            line.lstrip().startswith(marker)
            for line in data.splitlines()
            for marker in PRIVATE_KEY_MARKERS
        ):
            private_key_candidates.append(record["path"])
        # Restrict this to an actual non-empty assignment at the start of a
        # content line.  Searching for the bare token makes this scanner flag
        # its own detection constants, which prevents a legitimate package
        # from being built without finding a credential.
        if PROVIDER_CREDENTIAL_ASSIGNMENT.search(data):
            provider_credential_candidates.append(record["path"])
    return {
        "provider_credential_assignment_candidates": provider_credential_candidates,
        "private_key_material_candidates": private_key_candidates,
    }


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    with tarfile.open(archive, "r:") as handle:
        for member in handle.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"archive path escapes extraction root: {member.name}")
            if member.issym() or member.islnk():
                link_target = (destination / Path(member.name).parent / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise RuntimeError(f"archive link escapes extraction root: {member.name}")
        try:
            handle.extractall(destination, filter="data")
        except TypeError:
            handle.extractall(destination)


def _inventory(root: Path, expected: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    actual_paths: set[str] = set()
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        actual_paths.add(relative)
        source = expected.get(relative)
        if source is None:
            raise RuntimeError(f"archive contains path absent from Git tree: {relative}")
        file_mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
            kind = "symlink"
            size = len(target.encode("utf-8"))
        elif path.is_file():
            digest = _sha256(path)
            kind = "file"
            size = path.stat().st_size
        else:
            raise RuntimeError(f"unsupported extracted entry: {relative}")
        records.append(
            {
                "path": relative,
                "kind": kind,
                "size": size,
                "mode": f"{file_mode:04o}",
                "sha256": digest,
                **source,
            }
        )
    missing = sorted(set(expected) - actual_paths)
    if missing:
        raise RuntimeError(f"archive omitted {len(missing)} Git paths; first={missing[0]}")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise RuntimeError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    commit = _run(repo, "git", "rev-parse", f"{args.commit}^{{commit}}").stdout.strip()
    tree = _run(repo, "git", "rev-parse", f"{commit}^{{tree}}").stdout.strip()
    entries, gitlinks = _git_entries(repo, commit)
    prohibited = _prohibited_paths(set(entries))
    if prohibited:
        raise RuntimeError(f"prohibited tracked package paths: {prohibited[:10]}")
    sensitive = _sensitive_paths(set(entries))
    if sensitive:
        raise RuntimeError(f"tracked credential-path candidates: {sensitive[:10]}")

    archive = output_dir / f"aether-source-{commit[:8]}.tar"
    _run(repo, "git", "archive", "--format=tar", f"--output={archive}", commit)
    extract_root = output_dir / "extracted"
    _safe_extract(archive, extract_root)
    files = _inventory(extract_root, entries)
    content_audit = _content_audit(extract_root, files)
    if content_audit["provider_credential_assignment_candidates"]:
        raise RuntimeError(
            "tracked provider credential assignment candidates: "
            f"{content_audit['provider_credential_assignment_candidates'][:10]}"
        )

    manifest = {
        "schema": "aether_exact_git_source_package_v1",
        "source_commit": commit,
        "source_tree": tree,
        "construction": "git archive of the complete tracked commit tree",
        "mode_semantics": "Git executable-bit class is authoritative across extraction platforms",
        "tracked_entry_count": len(entries) + len(gitlinks),
        "tracked_blob_count": len(entries),
        "gitlink_count": len(gitlinks),
        "archive": {
            "name": archive.name,
            "size": archive.stat().st_size,
            "sha256": _sha256(archive),
        },
        "exclusions": {
            "untracked_files": "excluded by Git-tree construction",
            "generated_outputs": "excluded unless intentionally tracked as reference fixtures",
            "macos_sidecars": "rejected if tracked",
            "secrets": "untracked credentials excluded; tracked path/content audits recorded",
        },
        "tracked_credential_path_audit": {"status": "passed", "findings": sensitive},
        "tracked_content_audit": content_audit,
        "gitlinks": gitlinks,
        "files": files,
    }
    manifest_path = output_dir / "source-package-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt = {
        "status": "verified",
        "manifest": manifest_path.name,
        "manifest_sha256": _sha256(manifest_path),
        "source_commit": commit,
        "source_tree": tree,
        "tracked_entry_count": len(entries) + len(gitlinks),
        "tracked_blob_count": len(entries),
        "gitlink_count": len(gitlinks),
        "archive_sha256": manifest["archive"]["sha256"],
        "extracted_path": str(extract_root),
    }
    (output_dir / "source-package-receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
