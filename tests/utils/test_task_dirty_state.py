from __future__ import annotations

import pytest

from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git
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
                "--literal-pathspecs",
                "--no-optional-locks",
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


async def test_committable_task_paths_async_filters_definitively_ignored_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str, float, str | None]] = []

    async def fake_run(
        args: list[str],
        *,
        cwd: str,
        timeout: float,
        input_text: str | None = None,
    ) -> GitOk:
        calls.append((args, cwd, timeout, input_text))
        return GitOk(
            status="ok",
            argv=("git", *args),
            stdout="ignored.py\0",
            stderr="",
        )

    monkeypatch.setattr(daemon_git, "run", fake_run)

    assert await task_dirty_state.committable_task_paths_async(
        {"tracked.py", "ignored.py"},
        "/repo",
    ) == {"tracked.py"}
    assert calls == [
        (
            ["check-ignore", "--stdin", "-z"],
            "/repo",
            10.0,
            "ignored.py\0tracked.py\0",
        )
    ]


async def test_committable_task_paths_async_keeps_all_paths_when_git_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(
        args: list[str],
        *,
        cwd: str,
        timeout: float,
        input_text: str | None = None,
    ) -> GitFailed:
        return GitFailed(
            status="failed",
            argv=("git", *args),
            returncode=128,
            stdout="",
            stderr="fatal: unavailable",
        )

    monkeypatch.setattr(daemon_git, "run", fake_run)

    paths = {"tracked.py", "possibly-ignored.py"}
    assert await task_dirty_state.committable_task_paths_async(paths, "/repo") == paths


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


async def test_task_dirty_paths_async_uses_typed_status_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, set[str], float]] = []

    async def fake_status(
        cwd: str,
        paths: set[str],
        *,
        timeout: float,
    ) -> GitOk:
        calls.append((cwd, paths, timeout))
        return GitOk(
            status="ok",
            argv=("git", "status"),
            stdout=" M first.py\0?? third.py\0",
            stderr="",
        )

    monkeypatch.setattr(daemon_git, "status", fake_status)

    dirty = await task_dirty_state.task_dirty_paths_async(
        {"third.py", "second.py", "first.py"},
        "/repo",
    )

    assert dirty == {"first.py", "third.py"}
    assert calls == [
        ("/repo", {"third.py", "second.py", "first.py"}, 10.0),
    ]


async def test_task_dirty_paths_async_preserves_unavailable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_status(
        cwd: str,
        paths: set[str],
        *,
        timeout: float,
    ) -> GitTimeout:
        return GitTimeout(
            status="timeout",
            argv=("git", "status"),
            timeout=timeout,
        )

    monkeypatch.setattr(daemon_git, "status", fake_status)

    assert await task_dirty_state.task_dirty_paths_async({"first.py"}, "/repo") is None
