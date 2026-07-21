"""Vision perception lane: semantic extraction from binary artifacts.

When basic artifact inspection can only report metadata (``semantic_content_
available: false``) and the run was given a vision-capable model, the harness
transcribes the artifact through that model and returns the transcription as
clearly-labeled model-derived extraction evidence -- never as ground truth.

Without a vision model the lane reports the gap honestly (the receipt stays a
failed metadata-only inspection), so image tasks classify as a capability gap
instead of luring the solver into inspection loops.

Generic capability class; no task-specific logic.
"""
from __future__ import annotations

import base64
import posixpath
from typing import Any

from .ledger import Receipt

# Bounded payload: vision providers reject oversized images anyway; a giant
# artifact should be sampled/converted by the solver first.
_MAX_IMAGE_BYTES = 8_000_000

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

_TRANSCRIBE_PROMPT = (
    "Transcribe the semantic content of this image exactly and completely. "
    "If it contains code or text, reproduce it verbatim (preserve indentation, "
    "line breaks, and symbols; do not paraphrase or fix anything). If it is a "
    "diagram/chart/scene, describe every labeled element and value precisely. "
    "Output only the transcription/description, no commentary."
)


def media_type_for(path: str) -> str:
    return _MEDIA_TYPES.get(posixpath.splitext(path.lower())[1], "")


def needs_vision(base_receipt: Receipt) -> bool:
    """True when basic inspection could only produce non-semantic metadata."""
    payload = base_receipt.payload or {}
    metadata = payload.get("metadata") or {}
    return metadata.get("semantic_content_available") is False


def vision_transcribe_receipt(
    kernel: Any,
    action: Any,
    step: int,
    executor: Any,
    base_receipt: Receipt,
) -> Receipt | None:
    """Attempt vision transcription for a metadata-only inspection.

    Returns a replacement receipt on success or honest failure detail, or
    ``None`` when no vision route exists (caller keeps the base receipt).
    """
    hooks = getattr(kernel, "active_hooks", None)
    perceive = getattr(hooks, "perceive_image", None)
    if not callable(perceive):
        return None
    payload = dict(base_receipt.payload or {})
    path = str(payload.get("path", ""))
    media_type = media_type_for(path)
    if not media_type:
        return None

    read_bytes = getattr(executor, "read_file_bytes", None)
    if not callable(read_bytes):
        return None
    try:
        raw = read_bytes(path)
    except (OSError, FileNotFoundError) as exc:
        return Receipt(
            receipt_id=base_receipt.receipt_id + ":vision",
            step=step,
            kind="artifact_inspection",
            success=False,
            summary=f"vision perception could not read {path}: {exc}",
            failure_class="perception_required",
            payload=payload,
        )
    if len(raw) > _MAX_IMAGE_BYTES:
        payload["vision_skipped"] = f"artifact exceeds {_MAX_IMAGE_BYTES} bytes"
        return Receipt(
            receipt_id=base_receipt.receipt_id + ":vision",
            step=step,
            kind="artifact_inspection",
            success=False,
            summary=(
                f"vision perception skipped for {path}: artifact too large; "
                "downscale/convert it first"
            ),
            failure_class="perception_required",
            payload=payload,
        )

    encoded = base64.b64encode(raw).decode("ascii")
    try:
        transcription = str(perceive(_TRANSCRIBE_PROMPT, encoded, media_type))
    except Exception as exc:
        payload["vision_error"] = str(exc)[:500]
        return Receipt(
            receipt_id=base_receipt.receipt_id + ":vision",
            step=step,
            kind="artifact_inspection",
            success=False,
            summary=f"vision perception failed for {path}: {exc}",
            failure_class="perception_required",
            payload=payload,
        )

    metadata = dict(payload.get("metadata") or {})
    metadata["semantic_content_available"] = True
    metadata["semantic_content_status"] = "vision_model_transcription"
    payload.update({
        "metadata": metadata,
        "extracted_text": transcription,
        "extraction_route": "vision_model",
        # Provenance honesty: this is a model's reading of the artifact, not a
        # deterministic decode.  The verifier may independently re-derive it.
        "extraction_authority": "model_transcription_not_ground_truth",
        "image_bytes": len(raw),
        "media_type": media_type,
    })
    return Receipt(
        receipt_id=base_receipt.receipt_id + ":vision",
        step=step,
        kind="artifact_inspection",
        success=True,
        summary=(
            f"vision transcription of {path} ({len(raw)} bytes, {media_type}): "
            f"{len(transcription)} chars extracted"
        ),
        state_change=False,
        payload=payload,
    )
