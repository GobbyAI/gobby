"""Agent workspace commits recover, back off from and never leave Git index locks (#25285)."""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from gobby.agents import agent_workspaces
from gobby.agents.agent_workspaces import (
    commit_workspace,
    drain_workspace_commits,
    ensure_workspace,
    schedule_workspace_commit,
    workspace_anchor,
    workspace_git_dir,
)
from gobby.utils.daemon_git import GitResult, daemon_git

pytestmark = pytest.mark.unit

DAY_SECONDS = 24 * 60 * 60
HOLDER_WAIT_SECONDS = 10.0
ADD_TIMEOUT_SECONDS = 2.0
NOTES = "developer/session-1/notes.md"
SECRET_NAMES = ("bootstrap.yaml", "auth.json", ".secret_kek", "local_cli_token")
SECRET_FOLDERS = (
    "developer/session-1",
    "code-reviewer/session-2/gobby-pytest-home-x",
    "developer/session-3/evidence-probe/home/providers/codex",
)
# The first run parks inside the clean filter, and so inside `git add`'s index.lock, until the
# test touches `release`; later runs pass straight through.
HOLD_FILTER = (
    "if [ ! -e '{started}' ]; then touch '{started}'; "
    "while [ ! -e '{release}' ]; do sleep 0.05; done; fi; cat"
)


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _ws_argv(project_id: str) -> list[str]:
    return [
        "git",
        f"--git-dir={workspace_git_dir(project_id)}",
        f"--work-tree={workspace_anchor(project_id)}",
    ]


def _ws_git(project_id: str, *args: str) -> str:
    result = subprocess.run(
        [*_ws_argv(project_id), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _tracked(project_id: str) -> set[str]:
    return set(_ws_git(project_id, "ls-files").splitlines())


def _write(anchor: Path, relative: str, text: str) -> None:
    path = anchor / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _stale_lock(project_id: str) -> Path:
    lock = workspace_git_dir(project_id) / "index.lock"
    lock.touch()
    _age(lock, DAY_SECONDS)
    return lock


def _configure_filter(project_id: str, name: str, clean: str) -> None:
    """Route ``*.<name>`` files through a clean filter that runs while ``git add`` holds its lock."""
    _ws_git(project_id, "config", f"filter.{name}.clean", clean)
    attributes = workspace_git_dir(project_id) / "info" / "attributes"
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text(f"*.{name} filter={name}\n")


async def _trigger(project_id: str, trigger: str) -> None:
    """Fire one production commit trigger and wait for it; failures are logged, never raised."""
    schedule_workspace_commit(project_id, trigger)
    await drain_workspace_commits()


@contextmanager
def _live_lock_holder(project_id: str, scratch: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Run a real ``git add`` parked inside a clean filter, so it holds ``index.lock``."""
    started, release = scratch / "holder-started", scratch / "holder-release"
    _configure_filter(project_id, "hold", HOLD_FILTER.format(started=started, release=release))
    anchor = workspace_anchor(project_id)
    _write(anchor, "developer/session-1/held.hold", "held\n")
    holder = subprocess.Popen(
        [*_ws_argv(project_id), "add", "--", "developer/session-1/held.hold"],
        cwd=anchor,
        env=_git_env(),
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + HOLDER_WAIT_SECONDS
        while not started.exists():
            assert holder.poll() is None, "the holder git add exited before taking its lock"
            assert time.monotonic() < deadline, "the holder git add never reached its filter"
            time.sleep(0.05)
        yield holder
    finally:
        release.touch()
        try:
            holder.wait(timeout=HOLDER_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
        if holder.stderr is not None:
            holder.stderr.close()


@pytest.fixture
def project_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A fresh project in a fresh home, so no per-project state leaks between tests."""
    monkeypatch.setenv("GOBBY_HOME", str((tmp_path / "gobby-home").resolve()))
    return str(uuid.uuid4())


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the subcommand of every workspace git call, then run it."""
    calls: list[str] = []
    real_git = agent_workspaces._git

    async def spy(project_id: str, *args: str, **kwargs: Any) -> Any:
        calls.append(args[0])
        return await real_git(project_id, *args, **kwargs)

    monkeypatch.setattr(agent_workspaces, "_git", spy)
    return calls


async def test_stale_index_lock_is_recovered_with_a_warning(
    project_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    anchor = ensure_workspace(project_id)
    _write(anchor, NOTES, "one finding\n")
    lock = _stale_lock(project_id)
    caplog.set_level(logging.WARNING)

    sha = await commit_workspace(project_id, "set_handoff")

    assert sha is not None
    assert not lock.exists()
    assert NOTES in _tracked(project_id)
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == agent_workspaces.logger.name and record.levelno == logging.WARNING
    ]
    assert any("index.lock" in message for message in warnings), warnings


async def test_fresh_orphan_lock_is_not_removed(project_id: str) -> None:
    anchor = ensure_workspace(project_id)
    _write(anchor, NOTES, "one finding\n")
    lock = workspace_git_dir(project_id) / "index.lock"
    lock.touch()

    await _trigger(project_id, "set_handoff")

    assert lock.exists(), "a lock younger than the stale bound may belong to a git about to run"


async def test_live_holder_lock_is_kept_and_commits_back_off(
    project_id: str, tmp_path: Path, git_calls: list[str]
) -> None:
    ensure_workspace(project_id)

    with _live_lock_holder(project_id, tmp_path) as holder:
        lock = workspace_git_dir(project_id) / "index.lock"
        assert lock.exists(), "the holder git add should own index.lock"
        _age(lock, DAY_SECONDS)  # old enough to be stale, but a live git still owns it

        await _trigger(project_id, "set_handoff")
        first_adds = git_calls.count("add")
        for trigger in ("pre_compact", "cron", "set_handoff"):
            await _trigger(project_id, trigger)

        assert lock.exists(), "a lock held by a live git must never be removed"
        assert holder.poll() is None
        assert first_adds <= 2, "one blocked trigger may try add at most once and retry once"
        assert git_calls.count("add") == first_adds, "blocked triggers must back off, not retry"

    assert holder.returncode == 0, "removing the lock would have broken the holder's git add"


async def test_git_timeout_leaves_no_index_lock(
    project_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = ensure_workspace(project_id)
    _configure_filter(project_id, "slow", "sleep 30; cat")
    _write(anchor, "developer/session-1/slow.slow", "slow\n")
    real_run = daemon_git.run

    async def short_add_timeout(args: Sequence[str], **kwargs: Any) -> GitResult:
        if "add" in args:
            kwargs["timeout"] = ADD_TIMEOUT_SECONDS
        return await real_run(args, **kwargs)

    monkeypatch.setattr(daemon_git, "run", short_add_timeout)

    await _trigger(project_id, "set_handoff")

    # daemon_git SIGKILLs the process group on timeout, so git never runs its lock cleanup.
    assert not (workspace_git_dir(project_id) / "index.lock").exists()


async def test_secret_named_files_are_never_staged(project_id: str) -> None:
    anchor = ensure_workspace(project_id)
    _write(anchor, NOTES, "one finding\n")
    for folder in SECRET_FOLDERS:
        for name in SECRET_NAMES:
            _write(anchor, f"{folder}/{name}", f"SECRET-{name}-{folder}\n")

    sha = await commit_workspace(project_id, "set_handoff")

    assert sha is not None
    tracked = _tracked(project_id)
    assert NOTES in tracked
    assert [path for path in tracked if Path(path).name in SECRET_NAMES] == []
    assert "SECRET-" not in _ws_git(project_id, "log", "--all", "-p")


async def test_already_tracked_secret_is_not_restaged(project_id: str) -> None:
    anchor = ensure_workspace(project_id)
    secret = "developer/session-3/evidence-probe/home/providers/codex/auth.json"
    _write(anchor, secret, "SECRET-SEEDED\n")
    _ws_git(project_id, "add", "--", secret)
    _ws_git(
        project_id,
        *("-c", "user.name=seed", "-c", "user.email=seed@localhost"),
        *("commit", "--quiet", "-m", "seed a fixture the old committer tracked"),
    )
    _write(anchor, secret, "SECRET-MODIFIED\n")
    _write(anchor, NOTES, "one finding\n")

    sha = await commit_workspace(project_id, "set_handoff")

    assert sha is not None
    assert NOTES in _tracked(project_id)
    assert "SECRET-MODIFIED" not in _ws_git(project_id, "log", "--all", "-p")
