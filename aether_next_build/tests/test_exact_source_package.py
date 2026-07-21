"""Regression coverage for the exact-source package content audit."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _builder_module():
    source = Path(__file__).resolve().parents[2] / "scripts" / "build_exact_source_package.py"
    spec = importlib.util.spec_from_file_location("build_exact_source_package", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_content_audit_does_not_flag_its_own_detection_literals(tmp_path: Path) -> None:
    builder = _builder_module()
    copied_source = tmp_path / "scripts" / "build_exact_source_package.py"
    copied_source.parent.mkdir()
    copied_source.write_bytes(
        (Path(__file__).resolve().parents[2] / "scripts" / "build_exact_source_package.py").read_bytes()
    )

    audit = builder._content_audit(
        tmp_path,
        [{"kind": "file", "path": "scripts/build_exact_source_package.py"}],
    )

    assert audit["provider_credential_assignment_candidates"] == []
    assert audit["private_key_material_candidates"] == []


def test_content_audit_rejects_nonempty_provider_credential_assignment(tmp_path: Path) -> None:
    builder = _builder_module()
    env_file = tmp_path / "fixture.env"
    env_file.write_text("AZURE_OPENAI_API_KEY=not-a-real-key\\n", encoding="utf-8")

    audit = builder._content_audit(
        tmp_path,
        [{"kind": "file", "path": "fixture.env"}],
    )

    assert audit["provider_credential_assignment_candidates"] == ["fixture.env"]
