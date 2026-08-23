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


def _write_ts_test(root: Path, source: str) -> Path:
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    path = tests_dir / "state.test.ts"
    path.write_text(source, encoding="utf-8")
    return path


def _write_go_test(root: Path, source: str) -> Path:
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    path = tests_dir / "user_test.go"
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
    assert payload["issues"][0]["occurrences"] == 1


def test_baseline_detects_additional_occurrence_of_same_issue() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        test_path = _write_test(
            root,
            """
def test_repeated_issue():
    assert True
""",
        )
        baseline_path = root / ".gobby" / "test-quality-baseline.json"
        write_result = runner.invoke(
            quality_command,
            ["audit", "tests", "--write-baseline", str(baseline_path)],
        )
        test_path.write_text(
            """
def test_repeated_issue():
    assert True
    assert True
""",
            encoding="utf-8",
        )

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

    assert write_result.exit_code == 0
    assert result.exit_code == 1
    assert "New issues: 1" in result.output
    assert "Known baseline issues: 1" in result.output
    assert "test_repeated_issue:4" in result.output


def test_failing_audit_refuses_to_rewrite_baseline_without_override() -> None:
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
        original = '{"schema_version": 2, "issues": []}\n'
        baseline_path.write_text(original, encoding="utf-8")

        result = runner.invoke(
            quality_command,
            [
                "audit",
                "tests",
                "--baseline",
                str(baseline_path),
                "--fail-on-new",
                "--write-baseline",
                str(baseline_path),
            ],
        )
        persisted = baseline_path.read_text(encoding="utf-8")

    assert result.exit_code == 1
    assert "Failing new issues:" in result.output
    assert "refusing to write a baseline from a failing audit" in result.output
    assert "--allow-failing-baseline" in result.output
    assert persisted == original


def test_failing_audit_writes_baseline_with_explicit_override() -> None:
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
        baseline_path.write_text('{"schema_version": 2, "issues": []}\n', encoding="utf-8")

        result = runner.invoke(
            quality_command,
            [
                "audit",
                "tests",
                "--baseline",
                str(baseline_path),
                "--fail-on-new",
                "--write-baseline",
                str(baseline_path),
                "--allow-failing-baseline",
            ],
        )
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert result.exit_code == 1
    assert payload["issues"][0]["issue_code"] == "NO_ASSERTION"


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
        baseline_path.write_text('{"schema_version": 2, "issues": []}\n', encoding="utf-8")

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


def test_text_output_separates_below_threshold_new_issues_from_known_issues() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        test_path = _write_test(
            root,
            """
def test_mixed_severity():
    assert True
""",
        )
        baseline_path = root / ".gobby" / "test-quality-baseline.json"
        runner.invoke(
            quality_command,
            ["audit", "tests", "--write-baseline", str(baseline_path)],
        )
        test_path.write_text(
            """
import time

def test_mixed_severity():
    time.sleep(0.01)
    assert True
""",
            encoding="utf-8",
        )

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

    assert result.exit_code == 0
    assert "New issues below threshold:" in result.output
    assert "MEDIUM SLEEP_IN_TEST" in result.output
    assert "Known baseline issues:" in result.output
    assert "HIGH ASSERT_TRUE" in result.output


def test_default_min_severity_fails_on_new_medium_issue() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        test_path = _write_test(
            root,
            """
def test_sleeps():
    assert 1 == 1
""",
        )
        baseline_path = root / ".gobby" / "test-quality-baseline.json"
        runner.invoke(
            quality_command,
            ["audit", "tests", "--write-baseline", str(baseline_path)],
        )
        test_path.write_text(
            """
import time

def test_sleeps():
    time.sleep(0.01)
    assert 1 == 1
""",
            encoding="utf-8",
        )

        result = runner.invoke(
            quality_command,
            ["audit", "tests", "--baseline", str(baseline_path), "--fail-on-new"],
        )

    assert result.exit_code == 1
    assert "Failing new issues >= low: 1" in result.output
    assert "MEDIUM SLEEP_IN_TEST" in result.output


def test_fail_on_new_missing_baseline_treats_current_issues_as_new() -> None:
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
        baseline_path = root / ".gobby" / "missing-baseline.json"

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
    assert "Baseline missing; treating current issues as new" in result.output
    assert "Baseline mode: current-issues-as-new" in result.output
    assert "Failing new issues >= high: 1" in result.output


def test_fail_on_new_missing_baseline_passes_when_no_failing_issues() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(
            root,
            """
def test_asserts_behavior():
    assert 1 == 1
""",
        )
        baseline_path = root / ".gobby" / "missing-baseline.json"

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

    assert result.exit_code == 0
    assert "Baseline missing; treating current issues as new" in result.output
    assert "Failing new issues >= high: 0" in result.output


def test_missing_baseline_json_reports_status_and_warning() -> None:
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
        baseline_path = root / ".gobby" / "missing-baseline.json"

        result = runner.invoke(
            quality_command,
            [
                "audit",
                "tests",
                "--baseline",
                str(baseline_path),
                "--fail-on-new",
                "--format",
                "json",
            ],
        )
        payload = json.loads(result.output)

    assert result.exit_code == 1
    assert payload["diff"]["baseline"]["status"] == "missing"
    assert payload["diff"]["baseline"]["mode"] == "current-issues-as-new"
    assert payload["diff"]["baseline"]["message"] == (
        "Baseline missing; treating current issues as new"
    )
    assert "warnings" not in payload
    assert payload["report"]["warnings"] == [
        {
            "code": "BASELINE_MISSING",
            "message": "Baseline missing; treating current issues as new",
            "path": ".gobby/missing-baseline.json",
        }
    ]


def test_unsupported_language_warning_does_not_fail_on_new() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_go_test(
            root,
            """
package tests

func TestUser(t *testing.T) {
    t.Fatal("native validation owns this language")
}
""",
        )
        baseline_path = root / ".gobby" / "missing-baseline.json"

        result = runner.invoke(
            quality_command,
            [
                "audit",
                "tests/user_test.go",
                "--baseline",
                str(baseline_path),
                "--fail-on-new",
                "--min-severity",
                "high",
            ],
        )

    assert result.exit_code == 0
    assert "UNSUPPORTED_LANGUAGE" in result.output
    assert "Failing new issues >= high: 0" in result.output


def test_audit_counts_vitest_tests_with_expect_assertions() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_ts_test(
            root,
            """
import { describe, expect, it } from "vitest";

describe("setup state migration", () => {
  it("rewrites legacy neo4j keys", () => {
    const loaded = loadState();

    expect(loaded).toMatchObject({ falkordb_installed: false });
    expect(loaded).not.toHaveProperty("neo4j_installed");
  });
});
""",
        )

        result = runner.invoke(quality_command, ["audit", "tests/state.test.ts"])

    assert result.exit_code == 0
    assert "Files scanned: 1" in result.output
    assert "Tests scanned: 1" in result.output
    assert "Issues: 0" in result.output


def test_audit_counts_vitest_each_tests_with_expect_assertions() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_ts_test(
            root,
            """
import { expect, it } from "vitest";

it.each(["has space", "control char"])(
  "rejects invalid password %s",
  (password) => {
    submit(password);

    expect(runGobby).not.toHaveBeenCalled();
  },
);
""",
        )

        result = runner.invoke(quality_command, ["audit", "tests/state.test.ts"])

    assert result.exit_code == 0
    assert "Files scanned: 1" in result.output
    assert "Tests scanned: 1" in result.output
    assert "Issues: 0" in result.output


@pytest.mark.parametrize("requested_path", ["tests/helper.ts", "tests"])
def test_fail_on_new_rejects_zero_file_audits(requested_path: str) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        path = root / requested_path
        if path.suffix:
            path.parent.mkdir()
            path.write_text("export const helper = true;\n", encoding="utf-8")
        baseline_path = root / ".gobby" / "missing-baseline.json"

        result = runner.invoke(
            quality_command,
            [
                "audit",
                requested_path,
                "--baseline",
                str(baseline_path),
                "--fail-on-new",
            ],
        )

    assert result.exit_code == 1
    assert "Files scanned: 0" in result.output
    assert "NO_ANALYZABLE_FILES" in result.output
