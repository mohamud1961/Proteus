"""Helpers for bounded Letta Context-Bench filesystem probes."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LETTA_EVALS_ROOT = REPO_ROOT / "research/sources/codebases/letta-evals"
LETTA_FILESYSTEM_ROOT = LETTA_EVALS_ROOT / "letta-leaderboard/filesystem-agent"
LETTA_FILESYSTEM_FILES = LETTA_FILESYSTEM_ROOT / "files"
LETTA_FILESYSTEM_DATASET = LETTA_FILESYSTEM_ROOT / "datasets/filesystem_code.jsonl"
LETTA_FILESYSTEM_RUBRIC = LETTA_FILESYSTEM_ROOT / "rubric.txt"
_FALLBACK_LETTA_ROOT = REPO_ROOT / "eval_suite/fixtures/letta/letta/filesystem-agent"
if not LETTA_FILESYSTEM_ROOT.exists():
    LETTA_FILESYSTEM_ROOT = _FALLBACK_LETTA_ROOT
    LETTA_FILESYSTEM_FILES = LETTA_FILESYSTEM_ROOT / "files"
    LETTA_FILESYSTEM_DATASET = LETTA_FILESYSTEM_ROOT / "datasets/filesystem_code.jsonl"
    LETTA_FILESYSTEM_RUBRIC = LETTA_FILESYSTEM_ROOT / "rubric.txt"

_SELECTED_CASES = (
    (1, "easy"),
    (6, "medium"),
    (8, "hard"),
)

_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}


def letta_preflight() -> dict[str, Any]:
    blockers = []
    for path, label in (
        (LETTA_EVALS_ROOT, "letta_evals_root_missing"),
        (LETTA_FILESYSTEM_ROOT, "letta_filesystem_root_missing"),
        (LETTA_FILESYSTEM_FILES, "letta_filesystem_files_missing"),
        (LETTA_FILESYSTEM_DATASET, "letta_filesystem_dataset_missing"),
        (LETTA_FILESYSTEM_RUBRIC, "letta_filesystem_rubric_missing"),
    ):
        if not path.exists():
            blockers.append(label)
    return {
        "status": "pass" if not blockers else "blocked",
        "blockers": blockers,
        "letta_evals_root": str(LETTA_EVALS_ROOT),
        "filesystem_dataset": str(LETTA_FILESYSTEM_DATASET),
        "filesystem_files_root": str(LETTA_FILESYSTEM_FILES),
        "rubric_path": str(LETTA_FILESYSTEM_RUBRIC),
        "selected_cases": [
            {"dataset_index": index, "difficulty": difficulty}
            for index, difficulty in _SELECTED_CASES
        ],
    }


def selected_letta_filesystem_specs() -> list[dict[str, Any]]:
    if LETTA_FILESYSTEM_DATASET.exists():
        rows = [
            json.loads(line)
            for line in LETTA_FILESYSTEM_DATASET.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        files = _load_files()
    else:
        rows = _synthetic_rows()
        files = _synthetic_files()
    specs = []
    for dataset_index, expected_difficulty in _SELECTED_CASES:
        row = rows[dataset_index]
        extra = row.get("agent_args", {}).get("extra", {})
        difficulty = extra.get("difficulty", "unknown")
        if difficulty != expected_difficulty:
            raise ValueError(
                f"Expected Letta case {dataset_index} difficulty {expected_difficulty}, got {difficulty}"
            )
        question = row["input"].replace("{pwd}", "./letta/filesystem")
        prompt = (
            question
            + "\n\nUse shell inspection as needed. End with one direct final answer only."
        )
        specs.append(
            {
                "probe_id": f"letta_filesystem_{dataset_index:03d}_{difficulty}",
                "class": "letta_context_bench",
                "task_id": f"filesystem_code_{dataset_index:03d}",
                "difficulty": difficulty,
                "workspace_files": files,
                "task_prompt": prompt,
                "grade": {
                    "ground_truth": row["ground_truth"],
                    "question_type": extra.get("question_type", "unknown"),
                    "required_files": extra.get("required_files", []),
                },
            }
        )
    return specs


def grade_letta_filesystem_answer(result_text: str, ground_truth: str) -> dict[str, Any]:
    response = _normalize_text(result_text)
    truth = _normalize_text(ground_truth)
    exact = response == truth
    contains = truth in response
    numeric_match = _numeric_equivalent(response, truth)
    verdict = "pass" if exact or contains or numeric_match else "fail"
    return {
        "verdict": verdict,
        "matched_ground_truth": verdict == "pass",
        "ground_truth": ground_truth,
        "reason_codes": [] if verdict == "pass" else ["letta_ground_truth_mismatch"],
    }


def _load_files() -> dict[str, str]:
    files = {}
    for path in sorted(LETTA_FILESYSTEM_FILES.glob("*.txt")):
        files[f"/letta/filesystem/{path.name}"] = path.read_text(encoding="utf-8")
    return files


def _synthetic_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(9):
        difficulty = "easy"
        if index == 6:
            difficulty = "medium"
        if index == 8:
            difficulty = "hard"
        rows.append(
            {
                "input": "Inspect {pwd} and answer with the final total only.",
                "ground_truth": "50" if index == 6 else ("14" if index == 8 else str(index + 1)),
                "agent_args": {
                    "extra": {
                        "difficulty": difficulty,
                        "question_type": "numeric",
                        "required_files": [
                            "pets.txt",
                            "addresses.txt",
                            "bank_accounts.txt",
                            "credit_cards.txt",
                            "vehicles.txt",
                            "insurance_policies.txt",
                        ],
                    }
                },
            }
        )
    return rows


def _synthetic_files() -> dict[str, str]:
    return {
        "/letta/filesystem/pets.txt": "### pet-001 (owner: pers-0001)\nname: Nova\n",
        "/letta/filesystem/addresses.txt": "### addr-001 (owner: pers-0001)\nstate: Oregon\n",
        "/letta/filesystem/bank_accounts.txt": "### bank-001 (owner: pers-0001)\nbalance: 50\n",
        "/letta/filesystem/credit_cards.txt": "### card-001 (owner: pers-0001)\nlimit: 1000\n",
        "/letta/filesystem/vehicles.txt": "### veh-001 (owner: pers-0001)\nmodel: Hatchback\n",
        "/letta/filesystem/insurance_policies.txt": "### ins-001 (owner: pers-0001)\npolicy: Active\n",
    }


def _normalize_text(text: str) -> str:
    normalized = text.strip().lower()
    normalized = normalized.replace("\n", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    for word, digit in _NUMBER_WORDS.items():
        normalized = re.sub(rf"\b{word}\b", digit, normalized)
    return normalized


def _numeric_equivalent(response: str, truth: str) -> bool:
    response_number = _extract_decimal(response)
    truth_number = _extract_decimal(truth)
    if response_number is None or truth_number is None:
        return False
    return response_number == truth_number


def _extract_decimal(text: str) -> Decimal | None:
    cleaned = text.replace(",", "")
    matches = re.findall(r"-?\$?\d+(?:\.\d+)?", cleaned)
    if not matches:
        return None
    try:
        return Decimal(matches[-1].replace("$", ""))
    except InvalidOperation:
        return None
