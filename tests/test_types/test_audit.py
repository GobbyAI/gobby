from __future__ import annotations

import shlex
import shutil
import signal
import sys
from pathlib import Path

import pytest

from gobby.test_types._mypy import MypyInvocationError, resolve_mypy_command, run_mypy
from gobby.test_types.audit import audit_types_paths


def _checker_command(tmp_path: Path, source: str) -> str:
    checker = tmp_path / "checker with spaces.py"
    checker.write_text(source, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(checker))}"


def test_audit_maps_requested_errors_and_filters_followed_imports(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text("def test_sample() -> None: ...\n", encoding="utf-8")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("value = 1\n", encoding="utf-8")
    command = _checker_command(
        tmp_path,
        "\n".join(
            [
                "import sys",
                "print('tests/test_sample.py:2: error: Test failure [arg-type]')",
                "print('src/app.py:1: error: Imported failure [assignment]')",
                "sys.exit(1)",
            ]
        ),
    )

    report = audit_types_paths((tests_dir,), root=tmp_path, mypy_command=command)

    assert report.files_scanned == 1
    assert report.tests_scanned == 0
    assert [(issue.path, issue.issue_code, issue.severity) for issue in report.issues] == [
        ("tests/test_sample.py", "mypy:arg-type", "high")
    ]
    assert report.issues[0].fingerprint == "tests/test_sample.py::::mypy:arg-type"


def test_audit_rejects_targets_outside_project_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"

    with pytest.raises(ValueError, match="outside the project root"):
        audit_types_paths((outside,), root=tmp_path, mypy_command="missing")


def test_audit_reports_zero_analyzable_files_without_invoking_mypy(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    report = audit_types_paths((empty,), root=tmp_path, mypy_command="definitely-missing")

    assert report.files_scanned == 0
    assert report.issues == ()
    assert [warning.code for warning in report.warnings] == ["NO_ANALYZABLE_FILES"]


def test_resolver_falls_through_missing_uv_to_path_mypy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "uv.lock").touch()

    def fake_which(executable: str) -> str | None:
        return "/tools/mypy" if executable == "mypy" else None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert resolve_mypy_command(tmp_path) == ("/tools/mypy",)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import sys\nprint('checker crashed', file=sys.stderr)\nsys.exit(2)\n", "checker crashed"),
        (
            "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n",
            f"exit code {-signal.SIGTERM}",
        ),
    ],
)
def test_run_mypy_reports_non_finding_failures(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    command = _checker_command(tmp_path, source)

    with pytest.raises(MypyInvocationError, match=expected):
        run_mypy(("tests",), root=tmp_path, mypy_command=command)


def test_run_mypy_reports_missing_checker_and_timeout(tmp_path: Path) -> None:
    with pytest.raises(MypyInvocationError, match="executable not found"):
        run_mypy(("tests",), root=tmp_path, mypy_command="definitely-missing-checker")

    command = _checker_command(tmp_path, "import time\ntime.sleep(5)\n")
    with pytest.raises(MypyInvocationError, match="timed out"):
        run_mypy(("tests",), root=tmp_path, mypy_command=command, timeout=0)
