"""Compatibility alias for harness.aether2.runtime.route_schemas.

Moved into the self-contained ``harness`` package during the public
restructure; this shim preserves the ``runner.schemas`` import path.
"""

from __future__ import annotations

import sys as _sys

from harness.aether2.runtime.route_schemas import *  # noqa: F401,F403
import harness.aether2.runtime.route_schemas as _canonical

_sys.modules[__name__] = _canonical
