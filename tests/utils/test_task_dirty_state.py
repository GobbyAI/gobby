from __future__ import annotations

import pytest

from gobby.workflows import task_dirty_state


def test_task_dirty_paths_batches_scoped_git_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str, int]] = []

    def fake_run_git_command(
        command: list[str],
        *,
        cwd: str,
        timeout: int,
    ) -> str:
        calls.append((command, cwd, timeout))
        return " M first.py\n?? third.py\n"

    monkeypatch.setattr(task_dirty_state, "run_git_command", fake_run_git_command)

    dirty = task_dirty_state.task_dirty_paths(
        {"third.py", "second.py", "first.py"},
        "/repo",
    )

    assert dirty == {"first.py", "third.py"}
    assert calls == [
        (
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "first.py",
                "second.py",
                "third.py",
            ],
            "/repo",
            10,
        )
    ]


def test_task_dirty_paths_keeps_leading_space_status_on_first_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_git_command(
        command: list[str],
        *,
        cwd: str,
        timeout: int,
    ) -> str:
        # run_git_command strips stdout, so a first line whose XY status starts
        # with a space (" M path") arrives as "M path".
        return "M crates/gclient/src/app/live.rs\n D docs/old.md\n?? notes.txt"

    monkeypatch.setattr(task_dirty_state, "run_git_command", fake_run_git_command)

    dirty = task_dirty_state.task_dirty_paths(
        {"crates/gclient/src/app/live.rs", "docs/old.md", "notes.txt"},
        "/repo",
    )

    assert dirty == {"crates/gclient/src/app/live.rs", "docs/old.md", "notes.txt"}
