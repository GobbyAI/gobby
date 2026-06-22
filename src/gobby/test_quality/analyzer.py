"""Test-quality analyzer entrypoints."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from gobby.test_quality._analyzer_common import (
    _PYTHON_SUFFIX,
    _RUST_SUFFIX,
    _SCRIPT_TEST_SUFFIXES,
    _deduplicate_issues,
    _relative_path,
)
from gobby.test_quality._analyzer_discovery import _discover_files
from gobby.test_quality._analyzer_python import _analyze_python_file
from gobby.test_quality._analyzer_rust import _analyze_rust_file
from gobby.test_quality._analyzer_script import _analyze_script_file
from gobby.test_quality.models import AuditIssue, AuditReport


def audit_paths(paths: Sequence[str | Path], *, root: str | Path | None = None) -> AuditReport:
    """Audit pytest-style tests under paths."""

    root_path = Path.cwd() if root is None else Path(root)
    root_path = root_path.resolve()
    requested_paths = tuple(str(path) for path in paths)
    discovery = _discover_files(paths, root=root_path)

    issues: list[AuditIssue] = []
    tests_scanned = 0
    for file_path in discovery.files:
        file_issues, file_test_count = analyze_file(file_path, root=root_path)
        issues.extend(file_issues)
        tests_scanned += file_test_count

    return AuditReport(
        root=str(root_path),
        paths=requested_paths,
        issues=tuple(_deduplicate_issues(issues)),
        files_scanned=len(discovery.files),
        tests_scanned=tests_scanned,
        warnings=discovery.warnings,
    )


def analyze_file(
    path: str | Path, *, root: str | Path | None = None
) -> tuple[list[AuditIssue], int]:
    """Audit one test file."""

    file_path = Path(path)
    root_path = Path.cwd() if root is None else Path(root)
    source = file_path.read_text(encoding="utf-8")
    relative_path = _relative_path(file_path, root_path)

    if file_path.suffix in _SCRIPT_TEST_SUFFIXES:
        return _analyze_script_file(source, relative_path)
    if file_path.suffix == _RUST_SUFFIX:
        return _analyze_rust_file(source, relative_path)
    if file_path.suffix == _PYTHON_SUFFIX:
        return _analyze_python_file(source, relative_path, filename=str(file_path))

    return _analyze_python_file(source, relative_path, filename=str(file_path))
