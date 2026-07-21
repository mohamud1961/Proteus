"""Truthful graders for the bounded measurement eval slice."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from eval_suite.adapters.letta_context_bench import grade_letta_filesystem_answer
from eval_suite.graders.measurement_contracts import (
    load_extract_moves_contract,
    load_financial_document_contract,
    load_regex_log_contract,
)
from eval_suite.adapters.terminalbench_paths import resolve_terminalbench_task_root


def grade_measurement_spec(
    *,
    spec: dict[str, Any],
    result: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    benchmark_class = spec["benchmark_class"]
    if benchmark_class == "letta_context_bench":
        return grade_letta_filesystem_answer(_assistant_text(result), spec["ground_truth"])
    if benchmark_class == "contextbench":
        return grade_contextbench_verified_answer(_assistant_text(result), spec["grade_row"])
    if benchmark_class == "bfcl_strict_ground_truth":
        return grade_bfcl_ground_truth_answer(_assistant_text(result), spec["ground_truth"])
    if benchmark_class == "terminalbench_repaired_closure":
        return grade_extract_moves_workspace(workspace, task_id=spec["task_id"])
    if benchmark_class == "terminalbench_public_regression":
        return grade_public_terminalbench_workspace(workspace, task_id=spec["task_id"])
    if benchmark_class == "measurement_completion_partial_progress":
        return grade_partial_progress_workspace(
            workspace=workspace,
            result_text=_assistant_text(result),
            artifact_relpath=spec["artifact_relpath"],
            expected_payload=spec["expected_payload"],
        )
    if benchmark_class == "measurement_completion_verifier_repair":
        return grade_verifier_repair_workspace(workspace=workspace, verifier_relpath=spec["verifier_relpath"])
    if benchmark_class == "measurement_context_work_pocket":
        return grade_work_pocket_handoff_workspace(
            workspace=workspace,
            result_text=_assistant_text(result),
            artifact_relpath=spec["artifact_relpath"],
            expected_total=spec["expected_total"],
            required_evidence_paths=spec["required_evidence_paths"],
        )
    hits = [token for token in spec["required_snippets"] if token.lower() in json.dumps(result.get("execution", {}), sort_keys=True).lower()]
    verdict = "pass" if len(hits) >= spec["min_hits"] else "fail"
    return {
        "verdict": verdict,
        "matched_snippets": hits,
        "required_snippet_count": len(spec["required_snippets"]),
        "reason_codes": [] if verdict == "pass" else ["required_snippets_missing"],
    }


def grade_contextbench_verified_answer(result_text: str, row: dict[str, str]) -> dict[str, Any]:
    parsed = _parse_structured_answer(result_text)
    expected_repo = str(row["original_inst_id"]).split("__", 1)[0]
    expected = {
        "original_inst_id": str(row["original_inst_id"]),
        "language": str(row["language"]),
        "status": str(row["status"]),
        "gold_context_length": str(row["gold_context_length"]),
        "commit": str(row["commit"]),
    }
    reason_codes = []
    matched = {}
    for key, value in expected.items():
        observed = parsed.get(key)
        ok = observed == value
        matched[key] = ok
        if not ok:
            reason_codes.append(f"contextbench_{key}_mismatch")
    repo_value = parsed.get("repo_or_file_family") or parsed.get("repo") or parsed.get("file_family")
    repo_ok = isinstance(repo_value, str) and expected_repo.lower() in repo_value.lower()
    matched["repo_or_file_family"] = repo_ok
    if not repo_ok:
        reason_codes.append("contextbench_repo_or_file_family_mismatch")
    verdict = "pass" if not reason_codes else "fail"
    return {
        "verdict": verdict,
        "matched_fields": matched,
        "parsed_answer": parsed,
        "reason_codes": reason_codes,
    }


def grade_extract_moves_workspace(workspace: Path, *, task_id: str) -> dict[str, Any]:
    contract = load_extract_moves_contract(str(_task_dir(task_id)))
    solution_path = workspace / "solution.txt"
    if not solution_path.exists():
        return {"verdict": "fail", "reason_codes": ["missing_solution_file"], "solution_path": str(solution_path)}
    actual = solution_path.read_text(encoding="utf-8")
    expected = contract["expected_solution"]
    similarity = _similarity_percent(actual, expected)
    verdict = "pass" if similarity >= 90.0 else "fail"
    return {
        "verdict": verdict,
        "reason_codes": [] if verdict == "pass" else ["solution_similarity_below_threshold"],
        "similarity_percent": similarity,
        "line_count": len([line for line in actual.splitlines() if line.strip()]),
    }


def grade_bfcl_ground_truth_answer(result_text: str, ground_truth: list[list[str]]) -> dict[str, Any]:
    expected_calls = [call for turn in ground_truth if isinstance(turn, list) for call in turn]
    observed_calls = _parse_bfcl_calls(result_text)
    normalized_expected = [_normalize_bfcl_call(call) for call in expected_calls]
    normalized_observed = [_normalize_bfcl_call(call) for call in observed_calls]
    reason_codes = []
    if not observed_calls:
        reason_codes.append("bfcl_no_calls_emitted")
    if len(normalized_observed) < len(normalized_expected):
        reason_codes.append("bfcl_missing_required_calls")
    if len(normalized_observed) > len(normalized_expected):
        reason_codes.append("bfcl_extra_calls_emitted")
    first_mismatch = None
    for index, (expected, observed) in enumerate(zip(normalized_expected, normalized_observed, strict=False)):
        if expected != observed:
            first_mismatch = index
            reason_codes.append("bfcl_order_or_arguments_mismatch")
            break
    if first_mismatch is None and len(normalized_expected) != len(normalized_observed):
        first_mismatch = min(len(normalized_expected), len(normalized_observed))
    return {
        "verdict": "pass" if normalized_expected == normalized_observed else "fail",
        "reason_codes": sorted(set(reason_codes)),
        "expected_calls": expected_calls,
        "observed_calls": observed_calls,
        "expected_call_count": len(expected_calls),
        "observed_call_count": len(observed_calls),
        "first_mismatch_index": first_mismatch,
    }


def grade_public_terminalbench_workspace(workspace: Path, *, task_id: str) -> dict[str, Any]:
    if task_id == "fix-git":
        return _grade_fix_git_workspace(workspace)
    if task_id == "regex-log":
        return _grade_regex_log_workspace(workspace)
    if task_id == "financial-document-processor":
        return _grade_financial_workspace(workspace)
    raise ValueError(f"unsupported_terminalbench_task:{task_id}")


def grade_partial_progress_workspace(
    *,
    workspace: Path,
    result_text: str,
    artifact_relpath: str,
    expected_payload: dict[str, Any],
) -> dict[str, Any]:
    artifact_path = workspace / artifact_relpath
    if not artifact_path.exists():
        return {"verdict": "fail", "reason_codes": ["partial_progress_required_artifact_missing"], "artifact_path": str(artifact_path)}
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"verdict": "fail", "reason_codes": ["partial_progress_artifact_not_json"], "json_error": str(exc)}
    if payload != expected_payload:
        return {"verdict": "fail", "reason_codes": ["partial_progress_artifact_payload_mismatch"], "observed_payload": payload}
    reason_codes = []
    if artifact_relpath not in result_text:
        reason_codes.append("partial_progress_final_answer_missing_artifact_path")
    total_value = expected_payload.get("total")
    if total_value is not None and str(total_value) not in result_text:
        reason_codes.append("partial_progress_final_answer_missing_total")
    return {
        "verdict": "pass" if not reason_codes else "fail",
        "reason_codes": reason_codes,
        "artifact_path": str(artifact_path),
        "observed_payload": payload,
    }


def grade_verifier_repair_workspace(*, workspace: Path, verifier_relpath: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    verifier_path = workspace / verifier_relpath
    if not verifier_path.exists():
        return {"verdict": "fail", "reason_codes": ["verifier_script_missing"], "verifier_path": str(verifier_path)}
    verifier_command = str(verifier_path)
    verifier_cleanup_path: Path | None = None
    try:
        script_text = verifier_path.read_text(encoding="utf-8")
        normalized_text = script_text.replace("/app/", f"{workspace.as_posix().rstrip('/')}/")
        if normalized_text != script_text:
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".sh",
                prefix="measurement_verifier_",
                dir=str(workspace),
                delete=False,
            )
            try:
                handle.write(normalized_text)
            finally:
                handle.close()
            verifier_cleanup_path = Path(handle.name)
            verifier_command = str(verifier_cleanup_path)
    except OSError:
        pass
    proc = subprocess.run(
        ["bash", verifier_command],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if verifier_cleanup_path is not None:
        verifier_cleanup_path.unlink(missing_ok=True)
    return {
        "verdict": "pass" if proc.returncode == 0 else "fail",
        "reason_codes": [] if proc.returncode == 0 else ["verifier_rerun_failed"],
        "verifier_returncode": proc.returncode,
        "verifier_stdout": proc.stdout,
        "verifier_stderr": proc.stderr,
    }


def grade_work_pocket_handoff_workspace(
    *,
    workspace: Path,
    result_text: str,
    artifact_relpath: str,
    expected_total: int,
    required_evidence_paths: list[str],
) -> dict[str, Any]:
    artifact_path = workspace / artifact_relpath
    if not artifact_path.exists():
        return {"verdict": "fail", "reason_codes": ["work_pocket_artifact_missing"], "artifact_path": str(artifact_path)}
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"verdict": "fail", "reason_codes": ["work_pocket_artifact_not_json"], "json_error": str(exc)}
    reason_codes = []
    if _coerce_numeric(payload.get("verified_total")) != float(expected_total):
        reason_codes.append("work_pocket_total_mismatch")
    if str(payload.get("verification_status", "")).lower() != "verified":
        reason_codes.append("work_pocket_not_verified")
    evidence_paths = payload.get("evidence_paths")
    if not isinstance(evidence_paths, list) or sorted(str(path) for path in evidence_paths) != sorted(required_evidence_paths):
        reason_codes.append("work_pocket_evidence_paths_mismatch")
    if artifact_relpath not in result_text:
        reason_codes.append("work_pocket_final_answer_missing_artifact_path")
    if str(expected_total) not in result_text:
        reason_codes.append("work_pocket_final_answer_missing_total")
    return {
        "verdict": "pass" if not reason_codes else "fail",
        "reason_codes": reason_codes,
        "artifact_path": str(artifact_path),
        "observed_payload": payload,
    }


def _grade_fix_git_workspace(workspace: Path) -> dict[str, Any]:
    pairs = [
        (workspace / "resources/patch_files/about.md", workspace / "personal-site/_includes/about.md"),
        (workspace / "resources/patch_files/default.html", workspace / "personal-site/_layouts/default.html"),
    ]
    missing = [str(path) for pair in pairs for path in pair if not path.exists()]
    if missing:
        return {"verdict": "fail", "reason_codes": ["fix_git_required_files_missing"], "missing_paths": missing}
    mismatch = [str(dst) for src, dst in pairs if _md5_stripped(src) != _md5_stripped(dst)]
    return {"verdict": "pass" if not mismatch else "fail", "reason_codes": [] if not mismatch else ["fix_git_patch_mismatch"], "mismatched_paths": mismatch}


def _grade_regex_log_workspace(workspace: Path) -> dict[str, Any]:
    contract = load_regex_log_contract(str(_task_dir("regex-log")))
    regex_path = workspace / "regex.txt"
    if not regex_path.exists():
        return {"verdict": "fail", "reason_codes": ["regex_file_missing"]}
    pattern_text = regex_path.read_text(encoding="utf-8").strip()
    try:
        re.compile(pattern_text)
    except re.error as exc:
        return {"verdict": "fail", "reason_codes": ["regex_invalid"], "regex_error": str(exc)}
    matches = re.findall(pattern_text, "\n".join(contract["sample_logs"]), re.MULTILINE)
    verdict = "pass" if matches == contract["expected_dates"] else "fail"
    return {
        "verdict": verdict,
        "reason_codes": [] if verdict == "pass" else ["regex_expected_dates_mismatch"],
        "matched_dates": matches,
    }


def _grade_financial_workspace(workspace: Path) -> dict[str, Any]:
    contract = load_financial_document_contract(str(_task_dir("financial-document-processor")))
    invoices_dir = workspace / "invoices"
    other_dir = workspace / "other"
    documents_dir = workspace / "documents"
    summary_path = invoices_dir / "summary.csv"
    missing = [str(path) for path in (invoices_dir, other_dir, summary_path, documents_dir) if not path.exists()]
    if missing:
        return {"verdict": "fail", "reason_codes": ["financial_required_paths_missing"], "missing_paths": missing}
    invoice_hashes = {_sha512(path) for path in invoices_dir.iterdir() if path.is_file() and path.name != "summary.csv"}
    other_hashes = {_sha512(path) for path in other_dir.iterdir() if path.is_file()}
    if invoice_hashes != set(contract["invoice_hashes"]) - {"summary.csv"}:
        return {"verdict": "fail", "reason_codes": ["financial_invoice_hashes_mismatch"]}
    if other_hashes != set(contract["other_hashes"]):
        return {"verdict": "fail", "reason_codes": ["financial_other_hashes_mismatch"]}
    rows = list(csv.DictReader(summary_path.read_text(encoding="utf-8").splitlines()))
    if rows and list(rows[0].keys()) != ["filename", "total_amount", "vat_amount"]:
        return {"verdict": "fail", "reason_codes": ["financial_summary_headers_mismatch"]}
    if len(rows) != len(contract["expected_data"]):
        return {"verdict": "fail", "reason_codes": ["financial_summary_row_count_mismatch"]}
    expected = contract["expected_data"]
    for row in rows:
        filename = row["filename"]
        file_key = "total" if filename == "total" else _sha512(invoices_dir / filename)
        if file_key not in expected:
            return {"verdict": "fail", "reason_codes": ["financial_summary_unexpected_row"], "row": row}
        item = expected[file_key]
        actual_total = float(row["total_amount"])
        actual_vat = float(row["vat_amount"] or 0)
        expected_total = float(item["total_amount"])
        expected_vat = float(item["vat_amount"] or 0)
        if abs(actual_total - expected_total) >= 0.01:
            return {"verdict": "fail", "reason_codes": ["financial_total_amount_mismatch"], "row": row}
        if abs(actual_vat - expected_vat) >= 0.01:
            return {"verdict": "fail", "reason_codes": ["financial_vat_amount_mismatch"], "row": row}
    remaining_documents = [path.name for path in documents_dir.iterdir()]
    verdict = "pass" if not remaining_documents else "fail"
    return {"verdict": verdict, "reason_codes": [] if verdict == "pass" else ["financial_documents_not_empty"], "remaining_documents": remaining_documents}


def _assistant_text(result: dict[str, Any]) -> str:
    last = result.get("execution", {}).get("last_completion")
    if isinstance(last, dict) and isinstance(last.get("text"), str) and last["text"].strip():
        return last["text"]
    texts = []
    for step in result.get("execution", {}).get("steps", []):
        completion = step.get("completion") if isinstance(step, dict) else None
        text = completion.get("text") if isinstance(completion, dict) else None
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts[-1] if texts else ""


def _parse_structured_answer(text: str) -> dict[str, str]:
    stripped = text.strip()
    if not stripped:
        return {}
    for parser in (_parse_json_like, _parse_line_pairs):
        parsed = parser(stripped)
        if parsed:
            return parsed
    return {}


def _parse_json_like(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = ast.literal_eval(text)
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _parse_line_pairs(text: str) -> dict[str, str]:
    parsed = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        if key:
            parsed[key] = value.strip()
    return parsed


def _parse_bfcl_calls(text: str) -> list[str]:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"^\s*(?:\d+\.\s*|[-*]\s*)", "", line)
        if "(" in line and ")" in line:
            lines.append(line)
    return lines


def _normalize_bfcl_call(text: str) -> str:
    stripped = text.strip()
    try:
        parsed = ast.parse(stripped, mode="eval")
    except SyntaxError:
        return re.sub(r"\s+", " ", stripped)
    return ast.dump(parsed.body, annotate_fields=True, include_attributes=False)


def _coerce_numeric(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _task_dir(task_id: str) -> Path:
    return resolve_terminalbench_task_root(task_id)


def _md5_stripped(path: Path) -> str:
    return hashlib.md5(path.read_bytes().strip()).hexdigest()


def _sha512(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha512").hexdigest()


def _similarity_percent(actual: str, expected: str) -> float:
    distance = _levenshtein_distance(actual, expected)
    max_length = max(len(actual), len(expected))
    if max_length == 0:
        return 100.0
    return 100.0 * (1 - distance / max_length)


def _levenshtein_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        return _levenshtein_distance(right, left)
    if not right:
        return len(left)
    previous = range(len(right) + 1)
    for index, lchar in enumerate(left):
        current = [index + 1]
        for inner, rchar in enumerate(right):
            current.append(
                min(
                    previous[inner + 1] + 1,
                    current[inner] + 1,
                    previous[inner] + (lchar != rchar),
                )
            )
        previous = current
    return previous[-1]
