"""Shared asset resolution for BFCL adapters.

Primary path: official vendored BFCL v4 assets at official_vendor/.
Legacy path: synthetic stub fixtures at bfcl/ (NON-OFFICIAL, ping()-only stubs).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- Official vendored assets (REAL BFCL v4 multi-turn data) ---
OFFICIAL_VENDOR_ROOT = REPO_ROOT / "eval_suite/fixtures/bfcl/official_vendor"
OFFICIAL_VENDOR_DATA_DIR = OFFICIAL_VENDOR_ROOT / "data"
OFFICIAL_VENDOR_FUNC_SOURCE_DIR = OFFICIAL_VENDOR_ROOT / "func_source_code"
OFFICIAL_VENDOR_CHECKER_PATH = OFFICIAL_VENDOR_ROOT / "eval_checker/multi_turn_checker.py"

# Official data files (JSONL format, one entry per line)
OFFICIAL_DATA_FILES = (
    OFFICIAL_VENDOR_DATA_DIR / "BFCL_v4_multi_turn_base.json",
    OFFICIAL_VENDOR_DATA_DIR / "BFCL_v4_multi_turn_miss_func.json",
    OFFICIAL_VENDOR_DATA_DIR / "BFCL_v4_multi_turn_miss_param.json",
)
OFFICIAL_ANSWER_DIR = OFFICIAL_VENDOR_DATA_DIR / "possible_answer"

# --- Legacy candidates (NON-OFFICIAL synthetic stubs, kept for backward compat) ---
BFCL_SAMPLE_PATH_CANDIDATES = (
    REPO_ROOT / "eval_suite/fixtures/bfcl/bfcl/benchmark_samples/bfcl_v3_final.json",
    REPO_ROOT / "research/sources/codebases/deepagents/libs/evals/tests/evals/data/benchmark_samples/bfcl_v3_final.json",
    REPO_ROOT / "work/ledger/final_harness_eval_suite/adapter_fixtures/bfcl/benchmark_samples/bfcl_v3_final.json",
)
BFCL_API_DIR_CANDIDATES = (
    REPO_ROOT / "eval_suite/fixtures/bfcl/bfcl/bfcl_apis",
    REPO_ROOT / "research/sources/codebases/deepagents/libs/evals/tests/evals/data/bfcl_apis",
    REPO_ROOT / "work/ledger/final_harness_eval_suite/adapter_fixtures/bfcl/bfcl_apis",
)


def resolve_bfcl_samples_path() -> Path:
    """Legacy: resolve synthetic v3 sample path. Use load_official_cases() instead."""
    return _resolve_existing_path(BFCL_SAMPLE_PATH_CANDIDATES, asset_label="BFCL mirrored sample payload")


def resolve_bfcl_apis_dir() -> Path:
    """Legacy: resolve synthetic v3 API dir. Use OFFICIAL_VENDOR_FUNC_SOURCE_DIR instead."""
    return _resolve_existing_path(BFCL_API_DIR_CANDIDATES, asset_label="BFCL mirrored API directory")


def load_official_cases(*, limit: int | None = None) -> dict[str, dict[str, Any]]:
    """Load REAL official BFCL v4 multi-turn cases with ground truth merged in.

    Returns {case_id: case_dict} where each case_dict has keys:
    id, question, initial_config, involved_classes, ground_truth, etc.
    """
    if not official_vendor_available():
        raise FileNotFoundError(
            f"Official BFCL vendor assets not found at {OFFICIAL_VENDOR_ROOT}"
        )
    # Load questions from data files (JSONL)
    cases_by_id: dict[str, dict[str, Any]] = {}
    for data_file in OFFICIAL_DATA_FILES:
        if not data_file.exists():
            continue
        for line in data_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            entry = json.loads(line)
            cases_by_id[entry["id"]] = entry
    # Merge ground truth from possible_answer files
    if OFFICIAL_ANSWER_DIR.exists():
        for answer_file in sorted(OFFICIAL_ANSWER_DIR.iterdir()):
            if not answer_file.name.endswith(".json"):
                continue
            for line in answer_file.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                case_id = entry["id"]
                if case_id in cases_by_id:
                    cases_by_id[case_id]["ground_truth"] = entry["ground_truth"]
    if limit is not None:
        # Return first N cases that have ground truth
        result: dict[str, dict[str, Any]] = {}
        for case_id, case in cases_by_id.items():
            if case.get("ground_truth"):
                result[case_id] = case
                if len(result) >= limit:
                    break
        return result
    return cases_by_id


def official_vendor_available() -> bool:
    """Check if REAL official BFCL vendor assets are present and non-synthetic."""
    if not OFFICIAL_VENDOR_ROOT.exists():
        return False
    if not OFFICIAL_VENDOR_FUNC_SOURCE_DIR.exists():
        return False
    if not any(f.exists() for f in OFFICIAL_DATA_FILES):
        return False
    return True


def _is_synthetic_asset_path(path: Path) -> bool:
    """Detect if a path points to the synthetic/stub BFCL fixtures."""
    path_str = str(path)
    # The old synthetic stubs live under fixtures/bfcl/bfcl/
    if "fixtures/bfcl/bfcl/" in path_str:
        return True
    # Check for ping()-only ground truth (hallmark of synthetic data)
    if path.exists() and path.is_file() and path.suffix == ".json":
        try:
            content = path.read_text(encoding="utf-8")[:2000]
            payload = json.loads(content) if content.strip().startswith("{") else None
            if payload and isinstance(payload, dict):
                tasks = payload.get("tasks", [])
                if tasks and isinstance(tasks, list) and len(tasks) > 0:
                    gt = tasks[0].get("ground_truth", [])
                    if gt and isinstance(gt, list):
                        flat = str(gt)
                        if "ping(" in flat and "create_ticket" not in flat:
                            return True
        except (json.JSONDecodeError, OSError):
            pass
    return False


def bfcl_asset_preflight() -> dict[str, Any]:
    """Preflight check for BFCL assets.

    Uses official vendor as primary. Reports synthetic_assets_not_official
    blocker if only synthetic stubs are found.
    """
    has_official = official_vendor_available()
    # Legacy path check
    selected_sample_path = _first_existing_path(BFCL_SAMPLE_PATH_CANDIDATES)
    selected_apis_dir = _first_existing_path(BFCL_API_DIR_CANDIDATES)
    missing_paths = [
        str(path)
        for path in (*BFCL_SAMPLE_PATH_CANDIDATES, *BFCL_API_DIR_CANDIDATES)
        if not path.exists()
    ]
    blockers: list[str] = []
    if not has_official:
        blockers.append("missing_official_bfcl_vendor_assets")
    # Detect if we'd fall back to synthetic-only
    if not has_official and selected_sample_path is not None:
        if _is_synthetic_asset_path(selected_sample_path):
            blockers.append("synthetic_assets_not_official")
    return {
        "native_runtime_available": has_official and not blockers,
        "blocker_codes": blockers,
        "official_vendor_available": has_official,
        "official_vendor_root": str(OFFICIAL_VENDOR_ROOT),
        "missing_paths": missing_paths,
        "selected_sample_path": str(selected_sample_path) if selected_sample_path else "",
        "selected_apis_dir": str(selected_apis_dir) if selected_apis_dir else "",
        "sample_path_candidates": [str(path) for path in BFCL_SAMPLE_PATH_CANDIDATES],
        "api_dir_candidates": [str(path) for path in BFCL_API_DIR_CANDIDATES],
    }


def _first_existing_path(candidates: tuple[Path, ...]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resolve_existing_path(candidates: tuple[Path, ...], *, asset_label: str) -> Path:
    selected = _first_existing_path(candidates)
    if selected is not None:
        return selected
    checked = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"{asset_label} missing; checked {checked}")
