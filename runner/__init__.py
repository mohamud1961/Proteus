"""runner - Shared infrastructure for driving evals over the harness.

This package exposes the minimal substrate needed by eval_suite: contract
validators, schema aliases, path resolvers, and benchmark helpers.  There are
no legacy sub-packages (legacy_packets/, kernel/, mlpcp*) in this tree.
"""

from __future__ import annotations
