"""Tests for the test-quality CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gobby.test_quality.cli import test_quality as quality_command

pytestmark = pytest.mark.unit


def _write_test(root: Path, source: str) -> Path:
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    path = tests_dir / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_audit_text_output() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(
            root,
            """
def test_padding():
    assert True
""",
        )

        result = runner.invoke(quality_command, ["audit", "tests"])

    assert result.exit_code == 0
    assert "Test quality audit" in result.output
    assert "ASSERT_TRUE" in result.output


def test_audit_json_output_and_output_file() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(
            root,
            """
def test_no_assertion():
    cleanup()
""",
        )
        output_path = root / "report.json"

        result = runner.invoke(
            quality_command,
            ["audit", "tests", "--format", "json", "--output", str(output_path)],
        )
        payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert result.output == ""
    assert payload["report"]["issue_count_by_code"] == {"NO_ASSERTION": 1}


def test_write_baseline_and_current_baseline_passes() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(
            root,
            """
def test_no_assertion():
    cleanup()
""",
        )
        baseline_path = root / ".gobby" / "test-quality-baseline.json"

        write_result = runner.invoke(
            quality_command,
            ["audit", "tests", "--write-baseline", str(baseline_path)],
        )
        pass_result = runner.invoke(
            quality_command,
            [
                "audit",
                "tests",
                "--baseline",
                str(baseline_path),
                "--fail-on-new",
                "--min-severity",
                "high",
            ],
        )
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert write_result.exit_code == 0
    assert pass_result.exit_code == 0
    assert payload["issues"][0]["fingerprint"] == (
        "tests/test_sample.py::test_no_assertion::NO_ASSERTION"
    )


def test_fail_on_new_reports_synthetic_no_assertion() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(
            root,
            """
def test_no_assertion():
    cleanup()
""",
        )
        baseline_path = root / ".gobby" / "test-quality-baseline.json"
        baseline_path.parent.mkdir()
        baseline_path.write_text('{"schema_version": 1, "issues": []}\n', encoding="utf-8")

        result = runner.invoke(
            quality_command,
            [
                "audit",
                "tests",
                "--baseline",
                str(baseline_path),
                "--fail-on-new",
                "--min-severity",
                "high",
            ],
        )

    assert result.exit_code == 1
    assert "NO_ASSERTION" in result.output
    assert "Failing new issues >= high: 1" in result.output
