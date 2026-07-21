"""Task-contract loaders for the bounded Phase 6.5 measurement repair slice."""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=None)
def load_extract_moves_contract(task_dir: str) -> dict[str, Any]:
    module = _parse_module(Path(task_dir) / "tests/test_outputs.py")
    solution = _module_constant(module, "SOLUTION")
    if not isinstance(solution, str) or not solution.strip():
        raise ValueError("extract_moves_solution_missing")
    return {"expected_solution": solution}


@lru_cache(maxsize=None)
def load_regex_log_contract(task_dir: str) -> dict[str, Any]:
    module = _parse_module(Path(task_dir) / "tests/test_outputs.py")
    body = _function_body(module, "test_regex_matches_dates")
    sample_logs = _assigned_literal(body, "sample_logs")
    expected_dates = _assigned_literal(body, "expected_dates")
    if not isinstance(sample_logs, list) or not isinstance(expected_dates, list):
        raise ValueError("regex_log_contract_missing")
    return {"sample_logs": sample_logs, "expected_dates": expected_dates}


@lru_cache(maxsize=None)
def load_financial_document_contract(task_dir: str) -> dict[str, Any]:
    module = _parse_module(Path(task_dir) / "tests/test_outputs.py")
    invoice_hashes = _assigned_literal(
        _function_body(module, "test_invoices_moved_correctly"),
        "expected_original_documents",
    )
    other_hashes = _assigned_literal(
        _function_body(module, "test_other_documents_moved_correctly"),
        "expected_original_others",
    )
    expected_data = _assigned_literal(
        _function_body(module, "test_summary_csv_content"),
        "expected_data",
    )
    if not isinstance(invoice_hashes, set) or not isinstance(other_hashes, set):
        raise ValueError("financial_hash_contract_missing")
    if not isinstance(expected_data, dict):
        raise ValueError("financial_summary_contract_missing")
    return {
        "invoice_hashes": invoice_hashes,
        "other_hashes": other_hashes,
        "expected_data": expected_data,
    }


def _parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_constant(module: ast.Module, name: str) -> Any:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise ValueError(f"module_constant_missing:{name}")


def _function_body(module: ast.Module, name: str) -> list[ast.stmt]:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.body
    raise ValueError(f"function_missing:{name}")


def _assigned_literal(body: list[ast.stmt], name: str) -> Any:
    for node in body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise ValueError(f"assigned_literal_missing:{name}")
