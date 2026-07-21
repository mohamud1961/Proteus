from __future__ import annotations

import json
from pathlib import Path

from harness.aether2.control.execution_context import ExecutionContext
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.sessions import SessionRegistry


def _make_context(tmp_path: Path) -> ExecutionContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = ContainerExecutor(workspace_root=workspace)
    return ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(tmp_path / "state", backend=executor.backend, container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(tmp_path / "state", backend=executor.backend),
        raw_log_dir=tmp_path / "raw",
    )


def _stdout_json(envelope) -> dict[str, object]:  # noqa: ANN001
    return json.loads((envelope.stdout_head or "") + (envelope.stdout_tail or ""))


def test_inspect_artifact_uses_pdf_text_backend_when_available(monkeypatch, tmp_path: Path) -> None:
    ctx = _make_context(tmp_path)
    pdf_path = ctx.executor.workspace_root / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        "harness.aether2.control.execution_context._inspect_pdf_content",
        lambda target, max_chars: ("Invoice Total 123.45 VAT 12.34", "pdf_text", None),
    )

    envelope = ctx.inspect_artifact("sample.pdf", mode="pdf")
    payload = _stdout_json(envelope)
    outputs = payload["outputs"]

    assert outputs[0]["status"] == "content_available"
    assert outputs[0]["source"] == "pdf_text"
    assert "Invoice Total" in outputs[0]["text"]


def test_inspect_artifact_uses_image_ocr_backend_when_available(monkeypatch, tmp_path: Path) -> None:
    ctx = _make_context(tmp_path)
    image_path = ctx.executor.workspace_root / "sample.jpg"
    image_path.write_bytes(b"\xff\xd8\xff")

    monkeypatch.setattr(
        "harness.aether2.control.execution_context._inspect_image_content",
        lambda target, max_chars: ("Amount Due 44.10", None),
    )

    envelope = ctx.inspect_artifact("sample.jpg", mode="ocr")
    payload = _stdout_json(envelope)
    outputs = payload["outputs"]

    assert outputs[0]["status"] == "content_available"
    assert outputs[0]["source"] == "ocr"
    assert "Amount Due" in outputs[0]["text"]


def test_inspect_artifact_keeps_metadata_only_when_backends_unavailable(monkeypatch, tmp_path: Path) -> None:
    ctx = _make_context(tmp_path)
    image_path = ctx.executor.workspace_root / "sample.jpg"
    image_path.write_bytes(b"\xff\xd8\xff")

    monkeypatch.setattr(
        "harness.aether2.control.execution_context._inspect_image_content",
        lambda target, max_chars: (None, "OCR backend unavailable"),
    )

    envelope = ctx.inspect_artifact("sample.jpg", mode="ocr")
    payload = _stdout_json(envelope)
    outputs = payload["outputs"]

    assert outputs[0]["status"] == "metadata_only"
    assert outputs[0]["note"] == "OCR backend unavailable"


def test_ocr_image_text_surfaces_backend_init_error(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.jpg"
    image_path.write_bytes(b"\xff\xd8\xff")

    def _boom() -> None:
        raise RuntimeError("missing model blob")

    monkeypatch.setattr("harness.aether2.control.execution_context._rapidocr_engine", _boom)

    from harness.aether2.control.execution_context import _ocr_image_text

    text, note = _ocr_image_text(image_path, max_chars=200)
    assert text is None
    assert note == "OCR backend unavailable: missing model blob"


def test_inspect_artifact_reports_video_metadata_when_ffprobe_available(monkeypatch, tmp_path: Path) -> None:
    ctx = _make_context(tmp_path)
    video_path = ctx.executor.workspace_root / "sample.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    class _Completed:
        returncode = 0
        stdout = '{"streams":[{"width":1280,"height":720,"r_frame_rate":"30000/1001","nb_frames":"42"}],"format":{"duration":"3.5"}}'
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Completed())

    envelope = ctx.inspect_artifact("sample.mp4", mode="frames")
    payload = _stdout_json(envelope)
    outputs = payload["outputs"]

    assert outputs[0]["status"] == "metadata_only"
    assert outputs[0]["resolution"] == "1280x720"
    assert outputs[0]["duration_seconds"] == 3.5
    assert outputs[0]["frame_count"] == 42
    assert outputs[0]["sample_frames_extracted"] is False
