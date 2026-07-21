"""Canonical Aether package.

During consolidation, the implementation still lives under
``aether_next_build/aether_next``. This package exposes that implementation under
the stable ``aether`` import path without moving files or changing behavior.
"""
from __future__ import annotations

from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "aether_next_build" / "aether_next"
if not _SOURCE_ROOT.is_dir():  # pragma: no cover - import-time environment guard
    raise ImportError(f"canonical Aether source tree not found: {_SOURCE_ROOT}")

__path__ = [str(_SOURCE_ROOT)]

from .execution import MemoryExecutor
from .kernel import AetherNextKernel, KernelHooks, KernelResult
from .runtime_ir import (
    ActionRequest,
    CompiledRuntime,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)

__all__ = [
    "ActionRequest",
    "AetherNextKernel",
    "CompiledRuntime",
    "EnvMap",
    "KernelHooks",
    "KernelResult",
    "MemoryExecutor",
    "RuntimeConfigIR",
    "SolverTurn",
]
