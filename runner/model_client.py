"""Compatibility alias for older_variants.harness.aether2.runtime.model_routes."""

import sys as _sys

from older_variants.harness.aether2.runtime.model_routes import *  # noqa: F401,F403
import older_variants.harness.aether2.runtime.model_routes as _canonical

_sys.modules[__name__] = _canonical
