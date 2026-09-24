"""TDD gate path identity for a registered worktree of the main checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.workflows.condition_helpers import tdd_gate_open
from gobby.workflows.tdd_paths import _git_identity

_RELATIVE_TEST = "crates/x/tests.rs"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    test_file = main / _RELATIVE_TEST
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_acceptance() -> None:\n    assert True\n", encoding="utf-8")
    _git(main, "init")
    _git(main, "add", ".")
    _git(
        main,
        "-c",
        "user.email=tdd@example.com",
        "-c",
        "user.name=tdd",
        "commit",
        "-m",
        "acceptance test",
    )
    worktree = tmp_path / "registered"
    _git(main, "worktree", "add", str(worktree), "HEAD")
    return main, worktree


def test_worktree_absolute_acceptance_path_opens_gate_and_unrelated_suffix_stays_closed(
    tmp_path: Path,
) -> None:
    main, worktree = _repo_with_worktree(tmp_path)
    written = worktree / _RELATIVE_TEST
    variables = {
        "claimed_task_acceptance_test_paths": [_RELATIVE_TEST],
        "tdd_tests_written": [str(written)],
    }

    assert tdd_gate_open(variables, str(main)) is True

    unrelated = tmp_path / "unrelated" / _RELATIVE_TEST
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(written.read_text(encoding="utf-8"), encoding="utf-8")
    closed = {
        "claimed_task_acceptance_test_paths": [_RELATIVE_TEST],
        "tdd_tests_written": [str(unrelated)],
    }
    assert tdd_gate_open(closed, str(main)) is False


def test_same_suffix_in_another_git_repo_stays_closed(tmp_path: Path) -> None:
    main, _worktree = _repo_with_worktree(tmp_path)
    other = tmp_path / "other-repo" / _RELATIVE_TEST
    other.parent.mkdir(parents=True)
    other.write_text("def test_acceptance() -> None:\n    assert True\n", encoding="utf-8")
    _git(other.parent, "init")
    closed = {
        "claimed_task_acceptance_test_paths": [_RELATIVE_TEST],
        "tdd_tests_written": [str(other)],
    }

    assert tdd_gate_open(closed, str(main)) is False


def test_repeated_worktree_gate_checks_run_git_once_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _repo_with_worktree(tmp_path)
    _git_identity.cache_clear()
    counts: dict[str, int] = {}
    real_run = subprocess.run

    def _counting_run(
        args: list[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if "-C" in args:
            directory = args[args.index("-C") + 1]
            counts[directory] = counts.get(directory, 0) + 1
        return real_run(
            args,
            check=check,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
        )

    monkeypatch.setattr("gobby.workflows.tdd_paths.subprocess.run", _counting_run)
    variables = {
        "claimed_task_acceptance_test_paths": [_RELATIVE_TEST],
        "tdd_tests_written": [str(worktree / _RELATIVE_TEST)],
    }

    assert tdd_gate_open(variables, str(main)) is True
    assert tdd_gate_open(variables, str(main)) is True
    assert counts
    assert max(counts.values()) <= 1
