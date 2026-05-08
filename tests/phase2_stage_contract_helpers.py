"""Helpers for Phase 2 stage-manifest red contract tests."""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest


def _resolve_symbol(dotted_path: str) -> Any:
    module_path, _, attr_path = dotted_path.partition(":")
    try:
        value: Any = importlib.import_module(module_path)
    except ImportError as exc:
        pytest.fail(f"Missing Phase 2 module {module_path!r}: {exc}")

    if not attr_path:
        return value

    for attr in attr_path.split("."):
        if not hasattr(value, attr):
            pytest.fail(f"Missing Phase 2 symbol {dotted_path!r}")
        value = getattr(value, attr)
    return value


def assert_stage_contract(
    expectation: str,
    *,
    required_paths: Sequence[str] = (),
    required_symbols: Sequence[str] = (),
    required_text: Mapping[str, Sequence[str]] | None = None,
    forbidden_text: Mapping[str, Sequence[str]] | None = None,
) -> None:
    missing: list[str] = []
    violations: list[str] = []

    for raw_path in required_paths:
        path = Path(raw_path)
        if not path.exists():
            missing.append(f"path {raw_path}")

    for dotted_path in required_symbols:
        _resolve_symbol(dotted_path)

    for raw_path, snippets in (required_text or {}).items():
        path = Path(raw_path)
        if not path.exists():
            missing.append(f"path {raw_path}")
            continue
        text = path.read_text(encoding="utf-8")
        for snippet in snippets:
            if snippet not in text:
                missing.append(f"{raw_path} contains {snippet!r}")

    for raw_path, snippets in (forbidden_text or {}).items():
        path = Path(raw_path)
        if not path.exists():
            missing.append(f"path {raw_path}")
            continue
        text = path.read_text(encoding="utf-8")
        for snippet in snippets:
            if snippet in text:
                violations.append(f"{raw_path} still contains {snippet!r}")

    if missing or violations:
        details = "; ".join(missing + violations)
        pytest.fail(f"Phase 2 stage contract missing: {expectation}. Details: {details}")


def make_contract_test(
    expectation: str,
    *,
    required_paths: Sequence[str] = (),
    required_symbols: Sequence[str] = (),
    required_text: Mapping[str, Sequence[str]] | None = None,
    forbidden_text: Mapping[str, Sequence[str]] | None = None,
):
    def test() -> None:
        assert_stage_contract(
            expectation,
            required_paths=required_paths,
            required_symbols=required_symbols,
            required_text=required_text,
            forbidden_text=forbidden_text,
        )

    return test


def register_contract_tests(
    module_globals: dict[str, Any],
    contracts: Mapping[str, str],
    *,
    required_paths: Sequence[str] = (),
    required_symbols: Sequence[str] = (),
    required_text: Mapping[str, Sequence[str]] | None = None,
    forbidden_text: Mapping[str, Sequence[str]] | None = None,
) -> None:
    for name, expectation in contracts.items():
        if name in module_globals:
            continue
        test = make_contract_test(
            expectation,
            required_paths=required_paths,
            required_symbols=required_symbols,
            required_text=required_text,
            forbidden_text=forbidden_text,
        )
        test.__name__ = name
        test.__qualname__ = name
        module_globals[name] = test
