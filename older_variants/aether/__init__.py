"""OLD compatibility alias for the Proteus/Aether-Next runtime.

The implementation still lives under ``proteus/aether_next``. This
import path is retained for backward compatibility; new public code should use
Proteus and the canonical source path without changing behavior.
"""
from __future__ import annotations

from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "proteus" / "aether_next"
if not _SOURCE_ROOT.is_dir():  # pragma: no cover - import-time environment guard
    raise ImportError(f"canonical Aether source tree not found: {_SOURCE_ROOT}")

__path__ = [str(_SOURCE_ROOT)]

from proteus.aether_next.execution import MemoryExecutor
from proteus.aether_next.kernel import AetherNextKernel, KernelHooks, KernelResult
from proteus.aether_next.runtime_ir import (
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
