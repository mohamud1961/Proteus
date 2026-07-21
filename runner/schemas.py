"""Compatibility alias for older_variants.harness.aether2.runtime.route_schemas."""

import sys as _sys

from older_variants.harness.aether2.runtime.route_schemas import *  # noqa: F401,F403
import older_variants.harness.aether2.runtime.route_schemas as _canonical

_sys.modules[__name__] = _canonical
