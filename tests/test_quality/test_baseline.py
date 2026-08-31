from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gobby.test_quality.baseline import load_baseline, write_baseline
from gobby.test_quality.models import AuditIssue, AuditReport

pytestmark = pytest.mark.unit


def _report(*, message: str) -> AuditReport:
    return AuditReport(
        root="/project",
        paths=("tests",),
        issues=(
            AuditIssue(
                path="tests/test_example.py",
                test_name="test_example",
                issue_code="NO_ASSERTION",
                severity="high",
                line=3,
                message=message,
            ),
        ),
        files_scanned=1,
        tests_scanned=1,
    )


def test_interrupted_atomic_write_preserves_existing_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(_report(message="old"), baseline)
    original = baseline.read_bytes()

    def fail_replace(
        _source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        _dest: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        raise OSError("replace interrupted")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace interrupted"):
        write_baseline(_report(message="new"), baseline)

    assert baseline.read_bytes() == original
    assert tuple(tmp_path.glob(f".{baseline.name}.*.tmp")) == ()


def test_chmod_failure_preserves_existing_baseline_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(_report(message="old"), baseline)
    original = baseline.read_bytes()

    def fail_chmod(_path: Path, _mode: int) -> None:
        raise OSError("chmod interrupted")

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with pytest.raises(OSError, match="chmod interrupted"):
        write_baseline(_report(message="new"), baseline)

    assert baseline.read_bytes() == original
    assert tuple(tmp_path.glob(f".{baseline.name}.*.tmp")) == ()


def test_write_baseline_honors_process_umask(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    original_umask = os.umask(0o022)
    try:
        write_baseline(_report(message="current"), baseline)
    finally:
        os.umask(original_umask)

    assert baseline.stat().st_mode & 0o777 == 0o644


def test_load_baseline_rejects_unsupported_schema(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"schema_version": 999, "issues": []}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported audit baseline schema"):
        load_baseline(baseline)


@pytest.mark.parametrize("occurrences", [0, -1])
def test_load_baseline_rejects_non_positive_occurrences(
    tmp_path: Path,
    occurrences: int,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        (
            '{"schema_version": 2, "issues": ['
            f'{{"fingerprint": "sample", "occurrences": {occurrences}}}'
            "]}\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match="audit baseline issue occurrences must be positive integers"
    ):
        load_baseline(baseline)


def _scoped_report(
    *, root: Path, paths: tuple[str, ...], issues: tuple[AuditIssue, ...]
) -> AuditReport:
    return AuditReport(
        root=str(root),
        paths=paths,
        issues=issues,
        files_scanned=len(issues),
        tests_scanned=len(issues),
    )


def _issue(path: str, *, test_name: str = "test_one", message: str = "no assertion") -> AuditIssue:
    return AuditIssue(
        path=path,
        test_name=test_name,
        issue_code="NO_ASSERTION",
        severity="high",
        line=3,
        message=message,
    )


def _fingerprints(baseline: Path) -> set[str]:
    return {entry["fingerprint"] for entry in json.loads(baseline.read_text())["issues"]}


def test_narrow_write_keeps_entries_for_files_it_never_scanned(tmp_path: Path) -> None:
    """A scoped --write-baseline must not disarm the ratchet for the rest of the repo.

    A single-file audit only carries that file's issues, so writing the report
    verbatim deleted every other file's entries: auditing one path once took
    the repository baseline from 1,238 issues to 1.
    """
    baseline = tmp_path / "baseline.json"
    write_baseline(
        _scoped_report(
            root=tmp_path,
            paths=("tests",),
            issues=(_issue("tests/a/test_a.py"), _issue("tests/b/test_b.py")),
        ),
        baseline,
    )

    write_baseline(
        _scoped_report(
            root=tmp_path,
            paths=("tests/a/test_a.py",),
            issues=(_issue("tests/a/test_a.py", test_name="test_two"),),
        ),
        baseline,
    )

    assert _fingerprints(baseline) == {
        "tests/b/test_b.py::test_one::NO_ASSERTION",
        "tests/a/test_a.py::test_two::NO_ASSERTION",
    }


def test_narrow_write_drops_retired_entries_for_the_file_it_scanned(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(
        _scoped_report(
            root=tmp_path,
            paths=("tests",),
            issues=(_issue("tests/a/test_a.py"), _issue("tests/b/test_b.py")),
        ),
        baseline,
    )

    write_baseline(
        _scoped_report(root=tmp_path, paths=("tests/a/test_a.py",), issues=()),
        baseline,
    )

    assert _fingerprints(baseline) == {"tests/b/test_b.py::test_one::NO_ASSERTION"}


def test_directory_scope_covers_every_entry_beneath_it(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(
        _scoped_report(
            root=tmp_path,
            paths=("tests",),
            issues=(_issue("tests/a/test_a.py"), _issue("tests/b/test_b.py")),
        ),
        baseline,
    )

    write_baseline(
        _scoped_report(root=tmp_path, paths=("tests/a",), issues=()),
        baseline,
    )

    assert _fingerprints(baseline) == {"tests/b/test_b.py::test_one::NO_ASSERTION"}


def test_whole_root_audit_still_replaces_the_document(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(
        _scoped_report(
            root=tmp_path,
            paths=("tests",),
            issues=(_issue("tests/a/test_a.py"), _issue("tests/b/test_b.py")),
        ),
        baseline,
    )

    write_baseline(
        _scoped_report(root=tmp_path, paths=(".",), issues=(_issue("tests/c/test_c.py"),)),
        baseline,
    )

    assert _fingerprints(baseline) == {"tests/c/test_c.py::test_one::NO_ASSERTION"}


def test_absolute_audit_path_matches_relative_baseline_entries(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(
        _scoped_report(
            root=tmp_path,
            paths=("tests",),
            issues=(_issue("tests/a/test_a.py"), _issue("tests/b/test_b.py")),
        ),
        baseline,
    )

    write_baseline(
        _scoped_report(
            root=tmp_path,
            paths=(str(tmp_path / "tests" / "a" / "test_a.py"),),
            issues=(),
        ),
        baseline,
    )

    assert _fingerprints(baseline) == {"tests/b/test_b.py::test_one::NO_ASSERTION"}
