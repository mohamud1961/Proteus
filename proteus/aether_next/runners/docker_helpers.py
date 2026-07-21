"""Docker workspace seeding and grader layout detection helpers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Workspace seeding
# ---------------------------------------------------------------------------

_NOT_FOUND_MARKERS = ("No such file or directory", "not found", "Could not find")


def ensure_image_available(image_ref: str, *, pull_timeout_s: int | None = None) -> str | None:
    """Ensure *image_ref* is local, pulling it if needed.

    Returns ``None`` when the image is available; otherwise returns an
    evidence-bearing error string suitable for an environment-failure row.
    """
    inspect = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        capture_output=True,
        text=True, errors="replace",
        timeout=_timeout("AETHER_DOCKER_INSPECT_TIMEOUT_S", None, 60),
    )
    if inspect.returncode == 0:
        return None
    timeout = _timeout("AETHER_DOCKER_PULL_TIMEOUT_S", pull_timeout_s, 1200)
    try:
        pull = subprocess.run(
            ["docker", "pull", image_ref],
            capture_output=True,
            text=True, errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")[-2000:]
        stderr = str(exc.stderr or "")[-2000:]
        return f"docker pull timed out after {timeout}s: {(stdout + stderr).strip()}"
    if pull.returncode != 0:
        detail = (pull.stdout + pull.stderr).strip()[-4000:]
        return detail or "docker pull failed"
    return None


def seed_workspace_from_image(
    image_ref: str,
    workspace: Path,
    *,
    create_timeout_s: int | None = None,
    copy_timeout_s: int | None = None,
    remove_timeout_s: int | None = None,
) -> str | None:
    """Populate *workspace* with the image's ``/app`` contents.

    Uses ``docker create`` + ``docker cp`` (same technique as
    ``terminalbench_native._seed_workspace_from_image``).  Returns ``None``
    on success, or an error string on failure.
    """
    create_timeout = _timeout("AETHER_DOCKER_CREATE_TIMEOUT_S", create_timeout_s, 900)
    copy_timeout = _timeout("AETHER_DOCKER_COPY_TIMEOUT_S", copy_timeout_s, 900)
    remove_timeout = _timeout("AETHER_DOCKER_RM_TIMEOUT_S", remove_timeout_s, 60)
    create = subprocess.run(
        ["docker", "create", image_ref],
        capture_output=True,
        text=True, errors="replace",
        timeout=create_timeout,
    )
    if create.returncode != 0:
        return (create.stdout + create.stderr).strip() or "docker create failed"

    container_id = create.stdout.strip()
    try:
        for src_root in ("/app/.", "/workspace/."):
            cp = subprocess.run(
                ["docker", "cp", f"{container_id}:{src_root}", str(workspace)],
                capture_output=True,
                text=True, errors="replace",
                timeout=copy_timeout,
            )
            if cp.returncode == 0:
                return None
            stderr = (cp.stdout + cp.stderr).strip()
            if not any(marker in stderr for marker in _NOT_FOUND_MARKERS):
                return stderr or "docker cp failed"
        # Neither /app nor /workspace found -- empty workspace is valid.
        return None
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True, text=True, errors="replace", timeout=remove_timeout,
        )


def _timeout(env_name: str, explicit: int | None, default: int) -> int:
    if explicit is not None:
        return max(1, int(explicit))
    try:
        return max(1, int(os.environ.get(env_name, str(default))))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Grader layout detection
# ---------------------------------------------------------------------------


def detect_grader_command(task_dir: str) -> str:
    """Return the grader bash command for the given task directory.

    Two layouts:
    - ``mirrored_toml``: ``tests/test.sh`` exists -> ``bash /tests/test.sh``
    - ``official_yaml``: ``run-tests.sh`` exists  -> ``TEST_DIR=/tests bash /task/run-tests.sh``
    """
    task_path = Path(task_dir)
    if (task_path / "tests" / "test.sh").exists():
        return "bash /tests/test.sh"
    if (task_path / "run-tests.sh").exists():
        return "TEST_DIR=/tests bash /task/run-tests.sh"
    # Fallback: try test.sh anyway (most common layout).
    return "bash /tests/test.sh"
