from __future__ import annotations

import os
from pathlib import Path

import pytest

from gobby.test_quality.baseline import write_baseline
from gobby.test_quality.models import AuditIssue, AuditReport


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
