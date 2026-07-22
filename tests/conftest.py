"""Shared pytest fixtures/helpers for the Aether-2 test suite.

Additions only: this module exists to make the suite robust to host-load-induced
``BlockingIOError [Errno 35]`` failures when spawning subprocesses (seen in jobs,
sessions, vm_lifecycle_scripts, and executor tests on a busy machine). It does not
modify any production spawn code in `runner/aether2/*.py`.
"""

from __future__ import annotations

import errno
import subprocess
import time
from typing import Any, Callable, TypeVar

import pytest

T = TypeVar("T")

__all__ = ["spawn_with_retry"]

_DEFAULT_RETRIES = 5
_DEFAULT_BACKOFF_SEC = 0.2


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.EAGAIN:
        return True
    return False


def spawn_with_retry(
    fn: Callable[..., T],
    *args: Any,
    retries: int = _DEFAULT_RETRIES,
    backoff_sec: float = _DEFAULT_BACKOFF_SEC,
    **kwargs: Any,
) -> T:
    """Call `fn(*args, **kwargs)`, retrying on transient spawn-related errors.

    Retries `BlockingIOError` and `OSError` with `errno.EAGAIN` (commonly raised by
    `subprocess.run`/`subprocess.Popen` under host load when `fork`/`posix_spawn`
    transiently fails to allocate resources). Uses exponential backoff starting at
    `backoff_sec` (default 0.2s): 0.2s, 0.4s, 0.8s, 1.6s, 3.2s for the default 5 retries.
    """
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            if not _is_retryable(exc) or attempt >= retries - 1:
                raise
            time.sleep(backoff_sec * (2**attempt))
            attempt += 1


@pytest.fixture
def retrying_subprocess(monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch `subprocess.run`/`subprocess.Popen` (in `subprocess` and the given
    Aether-2 production modules) to retry transient `BlockingIOError`/`EAGAIN` spawn
    failures via `spawn_with_retry`.

    This patches only for the duration of the test (via monkeypatch) and does not
    alter any production source files. Returns a helper `apply(*modules)` that the
    test calls with the production module objects whose `subprocess` reference
    should also be wrapped (e.g. `runner.aether2.sessions`, `runner.aether2.jobs`,
    `runner.aether2.executor`).
    """
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def retrying_run(*args: Any, **kwargs: Any) -> Any:
        return spawn_with_retry(real_run, *args, **kwargs)

    def retrying_popen(*args: Any, **kwargs: Any) -> Any:
        return spawn_with_retry(real_popen, *args, **kwargs)

    def apply(*modules: Any) -> None:
        for module in modules:
            target = getattr(module, "subprocess", None)
            if target is None:
                continue
            monkeypatch.setattr(target, "run", retrying_run)
            monkeypatch.setattr(target, "Popen", retrying_popen)

    return apply
