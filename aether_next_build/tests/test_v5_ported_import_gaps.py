"""Explicit, deterministic gate for the V5-to-canonical import boundary.

This is a failing gate until the reference contracts are integrated into the
canonical package.  It prevents a missing test import from being mistaken for
an integrated V5 implementation.
"""

from importlib.util import find_spec


def test_v5_reference_package_is_not_a_parallel_harness() -> None:
    """The V5 package must be wired into canonical modules, never vendored."""
    assert find_spec("aether_next_upgrade") is None, (
        "aether_next_upgrade appeared as a parallel harness; port its contracts "
        "into aether_next instead"
    )


def test_v5_ported_tests_remain_visible() -> None:
    from pathlib import Path

    root = Path(__file__).with_name("v5_ported")
    names = sorted(root.glob("test_*.py"))
    assert len(names) == 23
    assert (root / "conftest.py").is_file()
    assert (root / "V5_PORT_MANIFEST.md").is_file()
