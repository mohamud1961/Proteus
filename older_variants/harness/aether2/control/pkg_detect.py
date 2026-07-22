"""Package-manager install detection for the §6.5 fact ledger.

Pure extraction from loop.py — zero behaviour change.
"""

from __future__ import annotations

import shlex

__all__ = [
    "_PACKAGE_MANAGER_PREFIXES",
    "_is_package_manager_install",
]

# Generic package-manager invocations (first 1-2 shell tokens). Used only to
# detect "this run_command was a package install" for the §6.5 fact ledger's
# ``installed_packages`` entry — no task-specific package names or logic.
_PACKAGE_MANAGER_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("apt-get", "install"),
    ("apt", "install"),
    ("pip", "install"),
    ("pip3", "install"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
    ("npm", "install"),
    ("npm", "i"),
    ("yarn", "add"),
    ("cargo", "install"),
    ("gem", "install"),
    ("go", "install"),
    ("brew", "install"),
    ("conda", "install"),
    ("apk", "add"),
    ("dnf", "install"),
    ("yum", "install"),
)


def _is_package_manager_install(command: str) -> bool:
    """Return True when *command* looks like a package-manager install invocation."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    for prefix in _PACKAGE_MANAGER_PREFIXES:
        if tuple(tokens[: len(prefix)]) == prefix:
            return True
    return False
