"""Tests for the test-types CLI."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from gobby.test_types.cli import test_types as types_command

pytestmark = pytest.mark.cli


def _write_checker(root: Path) -> str:
    checker = root / "fake mypy.py"
    checker.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "errors = 0",
                "for raw in sys.argv[1:]:",
                "    if raw.startswith('-'):",
                "        continue",
                "    target = Path(raw)",
                "    files = [target] if target.is_file() else target.rglob('*.py')",
                "    for path in files:",
                "        if path.name == 'fake mypy.py':",
                "            continue",
                "        for line_number, line in enumerate(path.read_text().splitlines(), 1):",
                "            if 'TYPE_ERROR' in line:",
                "                print(f'{path.as_posix()}:{line_number}: error: {line.strip()} [arg-type]')",
                "                errors += 1",
                "sys.exit(1 if errors else 0)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(checker))}"


def _write_test(root: Path, source: str, *, name: str = "test_sample.py") -> Path:
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    path = tests_dir / name
    path.write_text(source, encoding="utf-8")
    return path


def test_audit_supports_text_json_and_output_file() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(root, "# TYPE_ERROR first\n")
        checker = _write_checker(root)
        output = root / "report.json"

        text_result = runner.invoke(
            types_command,
            ["audit", "tests", "--mypy-command", checker],
        )
        json_result = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--format",
                "json",
                "--output",
                str(output),
            ],
        )
        payload = json.loads(output.read_text(encoding="utf-8"))

    assert text_result.exit_code == 0
    assert "Test types audit" in text_result.output
    assert "mypy:arg-type" in text_result.output
    assert json_result.exit_code == 0
    assert json_result.output == ""
    assert payload["report"]["issue_count_by_code"] == {"mypy:arg-type": 1}


def test_baseline_round_trip_and_additional_occurrence_fails() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        test_path = _write_test(root, "# TYPE_ERROR first\n")
        checker = _write_checker(root)
        baseline = root / "baseline.json"

        write_result = runner.invoke(
            types_command,
            ["audit", "tests", "--mypy-command", checker, "--write-baseline", str(baseline)],
        )
        pass_result = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--baseline",
                str(baseline),
                "--fail-on-new",
            ],
        )
        test_path.write_text("# TYPE_ERROR first\n# TYPE_ERROR second\n", encoding="utf-8")
        fail_result = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--baseline",
                str(baseline),
                "--fail-on-new",
            ],
        )

    assert write_result.exit_code == 0
    assert pass_result.exit_code == 0
    assert "Known baseline errors: 1" in pass_result.output
    assert "# TYPE_ERROR first" not in pass_result.output
    assert fail_result.exit_code == 1
    assert "New errors: 1" in fail_result.output
    assert "Known baseline errors: 1" in fail_result.output


def test_safe_regeneration_refuses_new_errors_unless_explicitly_allowed() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        test_path = _write_test(root, "# TYPE_ERROR first\n")
        checker = _write_checker(root)
        baseline = root / "baseline.json"
        runner.invoke(
            types_command,
            ["audit", "tests", "--mypy-command", checker, "--write-baseline", str(baseline)],
        )
        original = baseline.read_bytes()
        test_path.write_text("# TYPE_ERROR first\n# TYPE_ERROR second\n", encoding="utf-8")

        refused = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--baseline",
                str(baseline),
                "--fail-on-new",
                "--write-baseline",
                str(baseline),
            ],
        )
        after_refusal = baseline.read_bytes()
        allowed = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--baseline",
                str(baseline),
                "--fail-on-new",
                "--write-baseline",
                str(baseline),
                "--allow-failing-baseline",
            ],
        )
        safe_regeneration = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--baseline",
                str(baseline),
                "--fail-on-new",
                "--write-baseline",
                str(baseline),
            ],
        )
        payload = json.loads(baseline.read_text(encoding="utf-8"))

    assert refused.exit_code == 1
    assert "refusing to write a baseline from a failing audit" in refused.output
    assert after_refusal == original
    assert allowed.exit_code == 1
    assert safe_regeneration.exit_code == 0
    assert payload["issues"][0]["occurrences"] == 2


def test_missing_baseline_guards_and_zero_file_failure() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(root, "# TYPE_ERROR first\n")
        checker = _write_checker(root)
        missing = root / "missing.json"

        guard = runner.invoke(types_command, ["audit", "tests", "--fail-on-new"])
        missing_result = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--baseline",
                str(missing),
                "--fail-on-new",
            ],
        )
        empty = root / "empty"
        empty.mkdir()
        zero_result = runner.invoke(
            types_command,
            ["audit", "empty", "--baseline", str(missing), "--fail-on-new"],
        )

    assert guard.exit_code == 1
    assert "--fail-on-new requires --baseline" in guard.output
    assert missing_result.exit_code == 1
    assert "Baseline missing; treating current issues as new" in missing_result.output
    assert zero_result.exit_code == 1
    assert "NO_ANALYZABLE_FILES" in zero_result.output


def test_invocation_failure_does_not_write_baseline() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(root, "def test_ok() -> None: ...\n")
        baseline = root / "baseline.json"

        result = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                "definitely-missing-checker",
                "--write-baseline",
                str(baseline),
            ],
        )

    assert result.exit_code == 1
    assert "Error: mypy executable not found" in result.output
    assert baseline.exists() is False


def test_compact_output_details_only_new_errors() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(root, "# TYPE_ERROR known\n", name="test_known.py")
        checker = _write_checker(root)
        baseline = root / "baseline.json"
        runner.invoke(
            types_command,
            ["audit", "tests", "--mypy-command", checker, "--write-baseline", str(baseline)],
        )
        _write_test(root, "# TYPE_ERROR new\n", name="test_new.py")

        result = runner.invoke(
            types_command,
            [
                "audit",
                "tests",
                "--mypy-command",
                checker,
                "--baseline",
                str(baseline),
                "--fail-on-new",
            ],
        )

    assert result.exit_code == 1
    assert "Known baseline errors: 1" in result.output
    assert "# TYPE_ERROR new" in result.output
    assert "# TYPE_ERROR known" not in result.output


def test_real_mypy_reports_a_broken_typed_test() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        _write_test(
            root,
            "\n".join(
                [
                    "def accepts_int(value: int) -> None:",
                    "    pass",
                    "",
                    "def test_broken() -> None:",
                    "    accepts_int('wrong')",
                ]
            ),
        )

        result = runner.invoke(types_command, ["audit", "tests"])

    assert result.exit_code == 0
    assert "mypy:arg-type" in result.output
