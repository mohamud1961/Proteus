"""Compatibility alias for harness.aether2.runtime.route_schemas."""

import sys as _sys

from harness.aether2.runtime.route_schemas import *  # noqa: F401,F403
import harness.aether2.runtime.route_schemas as _canonical

_sys.modules[__name__] = _canonical
