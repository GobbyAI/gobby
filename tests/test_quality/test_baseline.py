from __future__ import annotations

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
