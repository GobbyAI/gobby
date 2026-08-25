"""Tests for the Python suppression ratchet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gobby.test_types.cli import test_types as types_command
from gobby.test_types.suppressions import scan_suppressions, write_suppression_baseline


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _baseline(root: Path, baseline: Path) -> None:
    scan = scan_suppressions((root,), root=root)
    write_suppression_baseline(baseline, scan.sites)


def test_scan_detects_directive_comments_and_ignores_prose_strings_and_generated_trees(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "tests" / "test_sample.py",
        '''"""A string containing # type: ignore[attr-defined]."""

TEXT = "# noqa: F401"
# Prose that discusses # noqa does not disable a check.
value = object()  # noqa: F401

def sample() -> None:
    result = value  # type: ignore[assignment]
    assert result is value
''',
    )
    _write(tmp_path / "generated.py", "# @generated\nvalue = 1  # noqa: F401\n")
    _write(tmp_path / "vendor" / "dependency.py", "value = 1  # noqa: F401\n")
    _write(tmp_path / "build" / "artifact.py", "value = 1  # type: ignore[misc]\n")
    _write(tmp_path / "tests" / "build" / "test_owned.py", "owned = 1  # noqa: E501\n")

    scan = scan_suppressions((tmp_path,), root=tmp_path)

    assert scan.files_scanned == 2
    assert [(site.directive, site.codes, site.symbol) for site in scan.sites] == [
        ("noqa", ("E501",), "<module>"),
        ("noqa", ("F401",), "<module>"),
        ("type: ignore", ("assignment",), "sample"),
    ]


def test_fingerprint_survives_line_and_formatting_movement(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_sample.py"
    _write(target, "def sample() -> None:\n    value = (1 + 2)  # noqa: F401\n")
    before = scan_suppressions((target,), root=tmp_path).sites[0]

    _write(
        target,
        "\n\ndef sample() -> None:\n    value=(\n        1 + 2\n    )  # noqa: F401\n",
    )
    after = scan_suppressions((target,), root=tmp_path).sites[0]

    assert before.line != after.line
    assert before.statement == after.statement
    assert before.fingerprint == after.fingerprint


def test_cli_accepts_unchanged_debt_and_rejects_changed_or_new_sites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "tests" / "test_sample.py"
    baseline = tmp_path / "baseline.json"
    _write(target, "value = 1  # noqa: F401\n")
    _baseline(tmp_path, baseline)
    runner = CliRunner()

    unchanged = runner.invoke(
        types_command,
        ["suppressions", ".", "--baseline", str(baseline)],
    )
    assert unchanged.exit_code == 0, unchanged.output
    assert "New: 0" in unchanged.output
    assert "Stale: 0" in unchanged.output

    _write(target, "value = 2  # noqa: F401\n")
    changed = runner.invoke(
        types_command,
        ["suppressions", ".", "--baseline", str(baseline)],
    )
    assert changed.exit_code == 1
    assert "New: 1" in changed.output
    assert "Stale: 1" in changed.output

    _write(target, "value = 1  # noqa: F401\n")
    _write(tmp_path / "tests" / "test_new.py", "other = 1  # type: ignore[misc]\n")
    added = runner.invoke(
        types_command,
        ["suppressions", ".", "--baseline", str(baseline)],
    )
    assert added.exit_code == 1
    assert "New: 1" in added.output
    assert "test_new.py" in added.output


def test_cli_requires_explicit_baseline_reduction_after_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "tests" / "test_sample.py"
    baseline = tmp_path / "baseline.json"
    _write(
        target,
        "first = 1  # noqa: F401\nsecond = 2  # type: ignore[assignment]\n",
    )
    _baseline(tmp_path, baseline)
    _write(target, "first = 1  # noqa: F401\n")
    runner = CliRunner()

    stale = runner.invoke(
        types_command,
        ["suppressions", ".", "--baseline", str(baseline)],
    )
    assert stale.exit_code == 1
    assert "Stale: 1" in stale.output

    reduced = runner.invoke(
        types_command,
        ["suppressions", ".", "--baseline", str(baseline), "--write-baseline"],
    )
    assert reduced.exit_code == 0, reduced.output
    assert json.loads(baseline.read_text(encoding="utf-8"))["site_count"] == 1

    _write(
        target,
        "first = 1  # noqa: F401\nthird = 3  # type: ignore[misc]\n",
    )
    before = baseline.read_text(encoding="utf-8")
    expansion = runner.invoke(
        types_command,
        ["suppressions", ".", "--baseline", str(baseline), "--write-baseline"],
    )
    assert expansion.exit_code == 1
    assert "refusing to expand" in expansion.output
    assert baseline.read_text(encoding="utf-8") == before


def test_duplicate_identical_sites_are_counted_as_separate_debt(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_sample.py"
    _write(
        target,
        """def sample() -> None:
    value = 1  # noqa: F401
    value = 1  # noqa: F401
""",
    )

    scan = scan_suppressions((target,), root=tmp_path)

    assert len(scan.sites) == 2
    assert scan.sites[0].fingerprint == scan.sites[1].fingerprint
