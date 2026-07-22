"""Artifact type detection lookup tables and probing."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
from typing import Any

ARTIFACT_TYPE_GUESSES = ("text", "json", "csv", "archive", "document", "image", "audio", "video", "binary", "unknown")

_COMPOUND_SUFFIX_TYPE_GUESSES: dict[str, str] = {
    ".jsonl": "json",
    ".ndjson": "json",
    ".tar.bz2": "archive",
    ".tar.gz": "archive",
    ".tar.xz": "archive",
    ".tbz2": "archive",
    ".tgz": "archive",
    ".txz": "archive",
}
_SUFFIX_TYPE_GUESSES: dict[str, str] = {
    ".7z": "archive",
    ".a": "binary",
    ".aac": "audio",
    ".avi": "video",
    ".bmp": "image",
    ".bz2": "archive",
    ".cfg": "text",
    ".class": "binary",
    ".csv": "csv",
    ".doc": "document",
    ".docx": "document",
    ".dll": "binary",
    ".dylib": "binary",
    ".exe": "binary",
    ".flac": "audio",
    ".gif": "image",
    ".gz": "archive",
    ".htm": "text",
    ".html": "text",
    ".ini": "text",
    ".jpeg": "image",
    ".jpg": "image",
    ".json": "json",
    ".log": "text",
    ".m4a": "audio",
    ".md": "text",
    ".mkv": "video",
    ".mov": "video",
    ".mp3": "audio",
    ".mp4": "video",
    ".mpeg": "video",
    ".mpg": "video",
    ".o": "binary",
    ".odt": "document",
    ".ogg": "audio",
    ".pages": "document",
    ".parquet": "binary",
    ".pdf": "document",
    ".pickle": "binary",
    ".png": "image",
    ".ppt": "document",
    ".pptx": "document",
    ".py": "text",
    ".pyc": "binary",
    ".rar": "archive",
    ".rtf": "document",
    ".sh": "text",
    ".so": "binary",
    ".svg": "image",
    ".tar": "archive",
    ".tbz2": "archive",
    ".tif": "image",
    ".tiff": "image",
    ".toml": "text",
    ".tsv": "csv",
    ".txt": "text",
    ".wav": "audio",
    ".webm": "video",
    ".webp": "image",
    ".xz": "archive",
    ".xml": "text",
    ".yaml": "text",
    ".yml": "text",
    ".zip": "archive",
}
_MIME_TYPE_GUESSES: dict[str, str] = {
    "application/gzip": "archive",
    "application/json": "json",
    "application/octet-stream": "binary",
    "application/pdf": "document",
    "application/vnd.rar": "archive",
    "application/x-7z-compressed": "archive",
    "application/x-bzip2": "archive",
    "application/x-tar": "archive",
    "application/x-xz": "archive",
    "application/zip": "archive",
    "audio/": "audio",
    "image/": "image",
    "text/csv": "csv",
    "text/": "text",
    "video/": "video",
}


def _artifact_suffix(path: Path) -> str:
    name = path.name.lower()
    for suffix in sorted(_COMPOUND_SUFFIX_TYPE_GUESSES, key=len, reverse=True):
        if name.endswith(suffix):
            return suffix
    suffix = path.suffix.lower()
    return suffix


def _type_guess_for_suffix(suffix: str) -> str:
    if suffix in _COMPOUND_SUFFIX_TYPE_GUESSES:
        return _COMPOUND_SUFFIX_TYPE_GUESSES[suffix]
    return _SUFFIX_TYPE_GUESSES.get(suffix, "unknown")


def _type_guess_for_mime_type(mime_type: str | None) -> str:
    if not mime_type:
        return "unknown"
    mime_type = mime_type.strip().lower()
    for prefix, guess in _MIME_TYPE_GUESSES.items():
        if prefix.endswith("/") and mime_type.startswith(prefix):
            return guess
        if mime_type == prefix:
            return guess
    return "unknown"


def _probe_mime_type(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    if shutil.which("file") is None:
        return None
    try:
        completed = subprocess.run(
            ["file", "-b", "--mime-type", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    mime_type = completed.stdout.strip().splitlines()[0].strip() if completed.stdout.strip() else ""
    return mime_type or None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guess_artifact_type(path: Path) -> dict[str, Any]:
    """Conservatively guess the artifact family from a visible suffix, with optional mime fallback."""
    suffix = _artifact_suffix(path)
    type_guess = _type_guess_for_suffix(suffix)
    reason_codes = [f"suffix:{suffix or 'none'}"]
    confidence = "high" if type_guess != "unknown" else "low"
    if type_guess == "unknown":
        mime_type = _probe_mime_type(path)
        mime_guess = _type_guess_for_mime_type(mime_type)
        if mime_guess != "unknown":
            type_guess = mime_guess
            confidence = "medium"
            reason_codes = [f"mime:{mime_type}", "mime_type_fallback"]
    return {
        "suffix": suffix,
        "type_guess": type_guess,
        "reason_codes": reason_codes,
        "confidence": confidence,
    }
