"""Test collection boundaries for the public code-only source package."""

from pathlib import Path
import sys

TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

import pytest


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    del config
    root = Path(__file__).resolve().parents[2]
    trace_roots = (
        root / "phase2_traces",
        root / "narrow_real_task_traces_20260630_043152",
    )
    has_historical_traces = any(path.is_dir() for path in trace_roots)
    if has_historical_traces:
        return
    reason = "historical replay trace corpus is external to the public code-only package"
    marker = pytest.mark.skip(reason=reason)
    for item in items:
        if item.fspath.basename == "test_historical_replay_engine.py":
            item.add_marker(marker)
