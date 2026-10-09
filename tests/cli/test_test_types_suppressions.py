"""Tests for the Python suppression ratchet."""

from __future__ import annotations

import json
import subprocess
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


@pytest.mark.parametrize(
    "target", [".", ".gobby/tmp", ".gobby/tmp/broken.py", ".gobby/scratchpads"]
)
def test_scan_excludes_gobby_scratch_even_when_explicitly_targeted(
    tmp_path: Path, target: str
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path / ".gitignore", ".gobby/tmp/\n.gobby/scratchpads/\n")
    _write(tmp_path / "src" / "owned.py", "value = 1  # noqa: F401\n")
    _write(tmp_path / ".gobby" / "scripts" / "owned.py", "value = 2  # noqa: F401\n")
    _write(tmp_path / ".gobby" / "tmp" / "broken.py", "value = (\n")
    _write(tmp_path / ".gobby" / "scratchpads" / "broken.py", "value = (\n")

    scan = scan_suppressions((target,), root=tmp_path)
    scoped = scan_suppressions(("src", ".gobby/scripts"), root=tmp_path)

    assert scan.errors == ()
    if target == ".":
        assert scan == scoped
        assert scan.files_scanned == 2
        assert [site.path for site in scan.sites] == [".gobby/scripts/owned.py", "src/owned.py"]
    else:
        assert scan.files_scanned == 0
        assert scan.sites == ()


def test_git_scan_includes_tracked_ignored_and_new_files_with_scoped_targets(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path / ".gitignore", "vendor/\n*.ignored.py\n")
    tracked = tmp_path / "vendor" / "owned.py"
    _write(tracked, "value = 1  # noqa: F401\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "vendor/owned.py"], check=True)
    _write(tmp_path / "src" / "new file.py", "value = 2  # noqa: F401\n")
    _write(tmp_path / "src" / "broken.ignored.py", "value = (\n")
    _write(tmp_path / "vendor" / "untracked.py", "value = (\n")

    scan = scan_suppressions((".",), root=tmp_path)
    scoped = scan_suppressions(("src", "vendor/owned.py", "src/new file.py"), root=tmp_path)
    subdirectory = scan_suppressions((".",), root=tmp_path / "src")

    assert scan == scoped
    assert scan.files_scanned == 2
    assert scan.errors == ()
    assert [site.path for site in scan.sites] == ["src/new file.py", "vendor/owned.py"]
    assert subdirectory.files_scanned == 1
    assert [site.path for site in subdirectory.sites] == ["new file.py"]


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_git_discovery_failure_does_not_fall_back_to_unfiltered_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def failed_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired("git", 30)
        return subprocess.CompletedProcess(["git"], 128, b"", b"fatal: permission denied")

    monkeypatch.setattr("gobby.test_types.suppressions.subprocess.run", failed_git)
    _write(tmp_path / "broken.py", "value = (\n")

    with pytest.raises(ValueError, match="Could not discover Git-visible Python source"):
        scan_suppressions((".",), root=tmp_path)


def test_scan_reports_each_malformed_source_and_continues(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path / "a_broken.py", "value = 1  # noqa: F401\nvalue = (\n")
    _write(tmp_path / "b_broken.py", 'value = """\n')
    _write(tmp_path / "z_valid.py", "value = 2  # noqa: F401\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)

    scan = scan_suppressions((".",), root=tmp_path)

    assert scan.files_scanned == 3
    assert [site.path for site in scan.sites] == ["z_valid.py"]
    assert len(scan.errors) == 2
    assert f"Could not tokenize Python source {tmp_path / 'a_broken.py'}:" in scan.errors[0]
    assert f"Could not tokenize Python source {tmp_path / 'b_broken.py'}:" in scan.errors[1]


@pytest.mark.parametrize("write_baseline", [False, True])
def test_cli_reports_source_errors_and_preserves_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_baseline: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "a_broken.py"
    baseline = tmp_path / "baseline.json"
    _write(target, "value = 1  # noqa: F401\n")
    _write(tmp_path / "z_valid.py", "value = 2  # noqa: F401\n")
    _baseline(tmp_path, baseline)
    original_baseline = baseline.read_bytes()
    _write(target, "value = (\n")
    arguments = ["suppressions", ".", "--baseline", str(baseline)]
    if write_baseline:
        arguments.append("--write-baseline")

    result = CliRunner().invoke(types_command, arguments)

    assert result.exit_code == 1
    assert "Files scanned: 2" in result.output
    assert "Suppressions: 1" in result.output
    assert f"Could not tokenize Python source {target}:" in result.output
    assert "New: 0" in result.output
    assert "Stale: 1" in result.output
    assert baseline.read_bytes() == original_baseline


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


def test_scoped_suppressions_check_ignores_outside_baseline_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "tests" / "test_first.py"
    second = tmp_path / "tests" / "test_second.py"
    baseline = tmp_path / "baseline.json"
    _write(first, "first = 1  # noqa: F401\n")
    _write(second, "second = 2  # noqa: F401\n")
    _baseline(tmp_path, baseline)

    result = CliRunner().invoke(
        types_command, ["suppressions", str(first), "--baseline", str(baseline)]
    )

    assert result.exit_code == 0, result.output
    assert "Stale: 0" in result.output


def test_scoped_suppression_baseline_write_preserves_outside_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "tests" / "test_first.py"
    second = tmp_path / "tests" / "test_second.py"
    baseline = tmp_path / "baseline.json"
    _write(first, "first = 1  # noqa: F401\n")
    _write(second, "second = 2  # noqa: F401\n")
    _baseline(tmp_path, baseline)
    _write(first, "first = 1\n")

    result = CliRunner().invoke(
        types_command,
        ["suppressions", str(first), "--baseline", str(baseline), "--write-baseline"],
    )

    assert result.exit_code == 0, result.output
    entries = json.loads(baseline.read_text(encoding="utf-8"))["sites"]
    assert len(entries) == 1
    assert entries[0]["path"] == "tests/test_second.py"


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

    before = baseline.read_text(encoding="utf-8")
    unchanged_write = runner.invoke(
        types_command,
        ["suppressions", ".", "--baseline", str(baseline), "--write-baseline"],
    )
    assert unchanged_write.exit_code == 1
    assert "strict debt reduction" in unchanged_write.output
    assert baseline.read_text(encoding="utf-8") == before

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
