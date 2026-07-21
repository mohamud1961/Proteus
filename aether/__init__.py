"""OLD compatibility alias for the Proteus/Aether-Next runtime.

The implementation still lives under ``aether_next_build/aether_next``. This
import path is retained for backward compatibility; new public code should use
Proteus and the canonical source path without changing behavior.
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
