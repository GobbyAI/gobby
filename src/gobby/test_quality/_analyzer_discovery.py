"""File discovery for test-quality analysis."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from gobby.test_quality._analyzer_common import (
    _PYTHON_SUFFIX,
    _RUST_SUFFIX,
    _SCRIPT_TEST_SUFFIXES,
    _UNSUPPORTED_TEST_LANGUAGE_SUFFIXES,
    _DiscoveryResult,
    _relative_path,
)
from gobby.test_quality.models import AuditWarning


def _discover_files(paths: Sequence[str | Path], *, root: Path) -> _DiscoveryResult:
    files: set[Path] = set()
    warnings: dict[str, AuditWarning] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        if path.is_file() and _is_analyzable_file(path):
            files.add(path.resolve())
            continue
        if path.is_file() and _is_unsupported_test_file(path):
            warning = _unsupported_language_warning(path.resolve(), root)
            warnings[warning.path or str(path)] = warning
            continue
        if path.is_dir():
            for candidate in path.rglob("*"):
                if _is_analyzable_file(candidate):
                    files.add(candidate.resolve())
                elif _is_unsupported_test_file(candidate):
                    warning = _unsupported_language_warning(candidate.resolve(), root)
                    warnings[warning.path or str(candidate)] = warning
    return _DiscoveryResult(
        files=tuple(sorted(files)),
        warnings=tuple(warnings[key] for key in sorted(warnings)),
    )


def _is_analyzable_file(path: Path) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts or ".venv" in parts or ".mypy_cache" in parts:
        return False
    if path.suffix == _PYTHON_SUFFIX:
        return True
    if path.suffix in _SCRIPT_TEST_SUFFIXES:
        return ".test." in path.name or ".spec." in path.name or "__tests__" in parts
    if path.suffix == _RUST_SUFFIX:
        return True
    return False


def _is_unsupported_test_file(path: Path) -> bool:
    if not path.is_file() or path.suffix not in _UNSUPPORTED_TEST_LANGUAGE_SUFFIXES:
        return False
    parts = set(path.parts)
    name = path.name.lower()
    stem = path.stem.lower()
    return (
        "tests" in parts
        or "__tests__" in parts
        or "test" in stem
        or "spec" in stem
        or name.endswith("_test.go")
        or name.endswith("test.java")
    )


def _unsupported_language_warning(path: Path, root: Path) -> AuditWarning:
    relative_path = _relative_path(path, root)
    return AuditWarning(
        code="UNSUPPORTED_LANGUAGE",
        path=relative_path,
        message=f"Unsupported test language for {relative_path}; audit attempted but unsupported",
    )
