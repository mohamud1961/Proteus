from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent

from conftest import spawn_with_retry


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_aether2_tournament.sh"


def _make_repo_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "tools").mkdir(parents=True)
    (repo_root / "tools" / "entrypoint.py").write_text(
        "#!/usr/bin/env python3\n",
        encoding="utf-8",
    )
    return repo_root


def _shell_quote_args(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _run_launcher(
    args: list[str],
    env: dict[str, str],
    *,
    cwd: Path,
    python3_log: Path,
    timeout_log: Path,
) -> subprocess.CompletedProcess[str]:
    command = dedent(
        f"""
        python3() {{
          printf 'ARGS:%s\\n' "$*" >> {shlex.quote(str(python3_log))}
          printf 'PYTHONPATH:%s\\n' "${{PYTHONPATH:-}}" >> {shlex.quote(str(python3_log))}
          rc="${{FAKE_PYTHON3_RC:-0}}"
          if [[ "$rc" != "0" ]]; then
            printf "ModuleNotFoundError: No module named 'runner'\\n" >&2
          fi
          return "$rc"
        }}
        timeout() {{
          printf 'ARGS:%s\\n' "$*" >> {shlex.quote(str(timeout_log))}
          printf 'PYTHONPATH:%s\\n' "${{PYTHONPATH:-}}" >> {shlex.quote(str(timeout_log))}

          task_id=""
          output_root=""
          prev=""
          for arg in "$@"; do
            case "$prev" in
              --task-id) task_id="$arg" ;;
              --output-root) output_root="$arg" ;;
            esac
            prev="$arg"
          done

          if [[ "${{FAKE_TIMEOUT_WRITE_ROW_JSON:-0}}" == "1" && -n "$task_id" && -n "$output_root" ]]; then
            row_dir="$output_root/20260613T000000Z/$task_id"
            mkdir -p "$row_dir"
            printf '{{"task_id":"%s"}}\\n' "$task_id" > "$row_dir/row.json"
          fi

          return "${{FAKE_TIMEOUT_RC:-1}}"
        }}
        set -- {_shell_quote_args(args)}
        source {shlex.quote(str(SCRIPT))}
        """
    ).lstrip()
    return spawn_with_retry(
        subprocess.run,
        ["bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


def _make_env(*, preflight_rc: str = "0", timeout_rc: str = "1", write_row_json: str = "0") -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    env["PYTHON_BIN"] = "python3"
    env["FAKE_PYTHON3_RC"] = preflight_rc
    env["FAKE_TIMEOUT_RC"] = timeout_rc
    env["FAKE_TIMEOUT_WRITE_ROW_JSON"] = write_row_json
    return env


def test_launcher_shell_syntax_check() -> None:
    result = spawn_with_retry(
        subprocess.run,
        ["bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_missing_entrypoint_fails_before_launch(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    task_ids = tmp_path / "task_ids.txt"
    task_ids.write_text("alpha\n", encoding="utf-8")
    output_root = tmp_path / "output"
    python3_log = tmp_path / "python3.log"
    timeout_log = tmp_path / "timeout.log"
    env = _make_env()

    result = _run_launcher(
        [
            "--repo-root",
            str(repo_root),
            "--task-ids-file",
            str(task_ids),
            "--output-root",
            str(output_root),
        ],
        env,
        cwd=tmp_path,
        python3_log=python3_log,
        timeout_log=timeout_log,
    )

    assert result.returncode == 64
    assert "--entrypoint is required" in result.stderr
    assert not timeout_log.exists()


def test_preflight_failure_stops_before_touching_corpus(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    task_ids = tmp_path / "task_ids.txt"
    task_ids.write_text("alpha\n", encoding="utf-8")
    output_root = tmp_path / "output"
    python3_log = tmp_path / "python3.log"
    timeout_log = tmp_path / "timeout.log"
    env = _make_env(preflight_rc="1")

    result = _run_launcher(
        [
            "--repo-root",
            str(repo_root),
            "--entrypoint",
            str(repo_root / "tools" / "entrypoint.py"),
            "--task-ids-file",
            str(task_ids),
            "--output-root",
            str(output_root),
        ],
        env,
        cwd=tmp_path,
        python3_log=python3_log,
        timeout_log=timeout_log,
    )

    assert result.returncode == 2
    assert "No module named 'runner'" in (result.stderr + result.stdout)
    assert not output_root.exists()
    assert not timeout_log.exists()
    assert python3_log.exists()
    python3_lines = python3_log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("PYTHONPATH:") and str(repo_root) in line for line in python3_lines)
    assert any("import runner.aether2.bridge_harbor" in line for line in python3_lines)


def test_dry_run_prints_plan_without_side_effects(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    task_ids = tmp_path / "task_ids.txt"
    task_ids.write_text("alpha\nbeta\n", encoding="utf-8")
    output_root = tmp_path / "output"
    python3_log = tmp_path / "python3.log"
    timeout_log = tmp_path / "timeout.log"
    env = _make_env()

    result = _run_launcher(
        [
            "--repo-root",
            str(repo_root),
            "--entrypoint",
            str(repo_root / "tools" / "entrypoint.py"),
            "--task-ids-file",
            str(task_ids),
            "--output-root",
            str(output_root),
            "--dry-run",
        ],
        env,
        cwd=tmp_path,
        python3_log=python3_log,
        timeout_log=timeout_log,
    )

    assert result.returncode == 0
    assert "dry-run preflight" in result.stdout
    assert "dry-run would run" in result.stdout
    assert "alpha" in result.stdout
    assert "beta" in result.stdout
    assert not output_root.exists()
    assert not timeout_log.exists()
    python3_lines = python3_log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("PYTHONPATH:") and str(repo_root) in line for line in python3_lines)


def test_invalid_launch_writes_marker_row_when_row_json_is_missing(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    task_ids = tmp_path / "task_ids.txt"
    task_ids.write_text("alpha\n", encoding="utf-8")
    output_root = tmp_path / "output"
    python3_log = tmp_path / "python3.log"
    timeout_log = tmp_path / "timeout.log"
    env = _make_env()

    result = _run_launcher(
        [
            "--repo-root",
            str(repo_root),
            "--entrypoint",
            str(repo_root / "tools" / "entrypoint.py"),
            "--task-ids-file",
            str(task_ids),
            "--output-root",
            str(output_root),
            "--attempt",
            "7",
            "--fail-fast-count",
            "99",
            "--fail-fast-elapsed-sec",
            "100",
        ],
        env,
        cwd=tmp_path,
        python3_log=python3_log,
        timeout_log=timeout_log,
    )

    assert result.returncode == 0
    progress = (output_root / "progress.tsv").read_text(encoding="utf-8").splitlines()
    invalid_launches = (output_root / "invalid_launches.tsv").read_text(encoding="utf-8").splitlines()
    assert len(progress) == 1
    assert progress[0].split("\t")[1] == "alpha"
    assert progress[0].split("\t")[2] == "1"
    assert len(invalid_launches) == 1
    assert invalid_launches[0].split("\t")[1] == "alpha"
    assert invalid_launches[0].split("\t")[4] == "invalid_launch"
    assert not list(output_root.rglob("row.json"))
    timeout_lines = timeout_log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("PYTHONPATH:") and str(repo_root) in line for line in timeout_lines)
    assert any("alpha" in line for line in timeout_lines)


def test_fail_fast_aborts_after_consecutive_fast_failures(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    task_ids = tmp_path / "task_ids.txt"
    task_ids.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    output_root = tmp_path / "output"
    python3_log = tmp_path / "python3.log"
    timeout_log = tmp_path / "timeout.log"
    env = _make_env()

    result = _run_launcher(
        [
            "--repo-root",
            str(repo_root),
            "--entrypoint",
            str(repo_root / "tools" / "entrypoint.py"),
            "--task-ids-file",
            str(task_ids),
            "--output-root",
            str(output_root),
            "--fail-fast-count",
            "3",
            "--fail-fast-elapsed-sec",
            "100",
        ],
        env,
        cwd=tmp_path,
        python3_log=python3_log,
        timeout_log=timeout_log,
    )

    assert result.returncode == 3
    assert "fail-fast threshold reached" in result.stderr
    timeout_lines = timeout_log.read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in timeout_lines if line.startswith("ARGS:")) == 3
    progress = (output_root / "progress.tsv").read_text(encoding="utf-8").splitlines()
    invalid_launches = (output_root / "invalid_launches.tsv").read_text(encoding="utf-8").splitlines()
    assert len(progress) == 3
    assert len(invalid_launches) == 3
    assert all("delta" not in line for line in timeout_lines)
    python3_lines = python3_log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("PYTHONPATH:") and str(repo_root) in line for line in python3_lines)
