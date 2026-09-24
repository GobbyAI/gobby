"""TDD gate path identity for a registered worktree of the main checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gobby.workflows.condition_helpers import tdd_gate_open

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
