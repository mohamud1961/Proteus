"""Exclusion proofs: the certified package cannot reach quarantined legacy code.

``reference_legacy`` holds the superseded contract-extraction architect mode and
task-family proof-contract analyzers.  These tests are falsifiable exclusion
gates: re-introducing an import, a module file, or an architect-mode branch
makes them fail.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

import pytest

import aether_next
from aether_next.run_adapter import ensure_certified_architect_mode

_CERTIFIED_ROOT = Path(aether_next.__file__).parent
_QUARANTINED_MODULE_NAMES = (
    "contract_compile",
    "contract_hooks",
)


def _certified_sources() -> list[Path]:
    return sorted(_CERTIFIED_ROOT.rglob("*.py"))


def test_certified_package_has_no_reference_legacy_imports() -> None:
    pattern = re.compile(r"^\s*(from|import)\s+reference_legacy\b", re.MULTILINE)
    offenders = [
        str(path) for path in _certified_sources()
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"certified modules import reference_legacy: {offenders}"


def test_quarantined_module_files_do_not_exist_in_certified_package() -> None:
    present = [
        name for name in _QUARANTINED_MODULE_NAMES
        if (_CERTIFIED_ROOT / f"{name}.py").exists()
    ]
    assert not present, f"quarantined modules re-appeared in aether_next: {present}"


def test_importing_all_certified_modules_never_loads_reference_legacy() -> None:
    # Run in a subprocess so this test cannot be poisoned by other tests
    # having already imported reference_legacy into this interpreter.
    code = (
        "import importlib, pkgutil, sys\n"
        "import aether_next\n"
        "for info in pkgutil.walk_packages(aether_next.__path__, 'aether_next.'):\n"
        "    importlib.import_module(info.name)\n"
        "loaded = [name for name in sys.modules if name.startswith('reference_legacy')]\n"
        "assert not loaded, f'reference_legacy loaded: {loaded}'\n"
        "legacy = [name for name in sys.modules if any(\n"
        "    name == f'aether_next.{q}' for q in (\n"
        "        'contract_compile', 'contract_hooks'))]\n"
        "assert not legacy, f'legacy modules loaded under aether_next: {legacy}'\n"
        "print('CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_CERTIFIED_ROOT.parent),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_certified_adapter_rejects_legacy_architect_modes() -> None:
    ensure_certified_architect_mode("workbench")
    for mode in ("ir", "contract"):
        with pytest.raises(ValueError, match="quarantined in reference_legacy"):
            ensure_certified_architect_mode(mode)


def test_reference_legacy_still_importable_as_reference() -> None:
    # Quarantine is exclusion from the certified path, not deletion of the
    # reference surface: replay/audit tooling may still import it directly.
    module = importlib.import_module("reference_legacy.proof_contract")
    assert hasattr(module, "analyze_proof_contract")
