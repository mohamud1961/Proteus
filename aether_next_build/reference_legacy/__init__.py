"""Quarantined legacy reference implementations.

Nothing in the certified ``aether_next`` package may import this package.
It preserves superseded mechanisms (contract-extraction architect mode and
task-family proof-contract analyzers) for replay/audit of historical runs
only.  ``tests/test_legacy_quarantine.py`` enforces the exclusion.
"""
