"""Agent workspace repository and daemon committer (addendum 9.1)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.agents import agent_workspaces
from gobby.agents.agent_workspaces import (
    bind_session_workspace,
    commit_workspace,
    drain_workspace_commits,
    ensure_workspace,
    register_agent_workspace_cron,
    schedule_workspace_commit,
    workspace_anchor,
    workspace_git_dir,
)
from gobby.hooks.event_handlers import _agent, _session_end
from gobby.mcp_proxy.tools.sessions import _terminal
from gobby.runner_init import orchestration
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from tests.agents.conftest import AGENT_TEST_MACHINE_ID

pytestmark = pytest.mark.unit

PROJECT = "11111111-1111-4111-8111-111111111111"
ONE_MIB = 1 << 20


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def _ws_git(project_id: str, *args: str) -> str:
    result = _git(
        f"--git-dir={workspace_git_dir(project_id)}",
        f"--work-tree={workspace_anchor(project_id)}",
        *args,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _tracked(project_id: str) -> set[str]:
    return set(_ws_git(project_id, "ls-tree", "-r", "--name-only", "HEAD").splitlines())


def _subjects(project_id: str) -> list[str]:
    return _ws_git(project_id, "log", "--format=%s").splitlines()


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = (tmp_path / "gobby-home").resolve()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    return home


def _checkouts(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout and a linked worktree of it."""
    main = tmp_path / "main"
    main.mkdir()
    for args in (
        ("init", "--initial-branch=trunk"),
        ("config", "user.name", "Workspace test"),
        ("config", "user.email", "ws@example.invalid"),
        ("commit", "--allow-empty", "-m", "initial"),
        ("worktree", "add", "-b", "linked", str(tmp_path / "linked")),
    ):
        assert _git(*args, cwd=main).returncode == 0
    return main, tmp_path / "linked"


async def test_anchor_shared_and_gitless(
    gobby_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, linked = _checkouts(tmp_path)

    monkeypatch.chdir(main)
    from_main = ensure_workspace(PROJECT)
    monkeypatch.chdir(linked)
    from_linked = ensure_workspace(PROJECT)

    root = gobby_home / "agents_workspaces"
    assert from_main == from_linked == workspace_anchor(PROJECT) == root / PROJECT
    assert workspace_git_dir(PROJECT) == root / f"{PROJECT}.git"
    # The work tree has no .git, so an agent's own git finds no repository there.
    assert not (from_main / ".git").exists()
    assert _git("rev-parse", "--git-dir", cwd=from_main).returncode != 0
    assert (from_main / "README.md").is_file()
    config = workspace_git_dir(PROJECT) / "config"
    assert _git("config", "--file", str(config), "core.bare").stdout.strip() == "false"
    worktree = _git("config", "--file", str(config), "core.worktree").stdout.strip()
    assert Path(worktree).resolve() == from_main.resolve()


async def test_commit_only_when_dirty_with_guards(gobby_home: Path) -> None:
    anchor = ensure_workspace(PROJECT)
    await commit_workspace(PROJECT, "timer")
    assert await commit_workspace(PROJECT, "timer") is None, "a clean tree makes no commit"

    folder = anchor / "developer" / "session-12"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("one finding\n")
    (folder / "huge.bin").write_bytes(b"x" * (ONE_MIB + 1))
    (folder / "limit.bin").write_bytes(b"x" * ONE_MIB)
    (folder / "link.md").symlink_to(folder / "notes.md")

    sha = await commit_workspace(PROJECT, "set_handoff")

    assert sha is not None
    assert _ws_git(PROJECT, "rev-parse", "HEAD") == sha
    tracked = _tracked(PROJECT)
    assert {"developer/session-12/notes.md", "developer/session-12/limit.bin"} <= tracked
    assert "developer/session-12/huge.bin" not in tracked
    assert "developer/session-12/link.md" not in tracked
    assert _subjects(PROJECT)[0] == "agents-ws: set_handoff developer"
    author = _ws_git(PROJECT, "log", "-1", "--format=%an <%ae>")
    assert author == "Gobby Coordinator <gobby-coordinator@localhost>"

    # Concurrent triggers serialize on the project lock: none fails on Git's index lock.
    for index in range(5):
        (folder / f"burst-{index}.md").write_text(f"{index}\n")
    results = await asyncio.gather(
        *(commit_workspace(PROJECT, "pre_compact") for _ in range(5)), return_exceptions=True
    )
    assert not [result for result in results if isinstance(result, BaseException)], results
    assert {f"developer/session-12/burst-{index}.md" for index in range(5)} <= _tracked(PROJECT)


class _Executor:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


async def test_trigger_failures_are_non_blocking(
    gobby_home: Path,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(project_id: str, trigger: str) -> str | None:
        raise RuntimeError(f"disk full during {trigger}")

    monkeypatch.setattr(agent_workspaces, "commit_workspace", broken)
    caplog.set_level(logging.WARNING)

    schedule_workspace_commit(PROJECT, "session_end")
    await drain_workspace_commits()
    assert "disk full during session_end" in caplog.text
    monkeypatch.setattr(agent_workspaces, "commit_workspace", commit_workspace)

    # Each trigger site schedules the commit and returns; none awaits or raises it.
    for trigger_site in (
        _terminal.register_terminal_tools,
        _agent.AgentEventHandlerMixin.handle_pre_compact,
        _session_end.SessionEndMixin.handle_session_end,
    ):
        assert "schedule_workspace_commit(" in inspect.getsource(trigger_site), trigger_site
    assert "register_agent_workspace_cron(" in inspect.getsource(orchestration.init_orchestration)

    # The nightly cron commits a dirty tree.
    storage = CronJobStorage(temp_db)
    executor = _Executor()
    assert register_agent_workspace_cron(cron_storage=storage, cron_executor=executor) == 1
    (handler_name, handler), *others = executor.handlers.items()
    assert others == []
    (job,) = [
        job
        for job in storage.list_jobs(is_system=True)
        if job.action_config.get("handler") == handler_name
    ]
    assert (job.schedule_type, job.cron_expr, job.action_type) == (
        "cron",
        "30 3 * * *",
        "handler",
    )
    anchor = ensure_workspace(PROJECT)
    (anchor / "orchestrator").mkdir()
    (anchor / "orchestrator" / "log.md").write_text("timer entry\n")
    await handler(job)
    assert "orchestrator/log.md" in _tracked(PROJECT)
    assert _subjects(PROJECT)[0] == "agents-ws: timer orchestrator"


@pytest.mark.asyncio
async def test_timer_bootstraps_the_first_workspace(
    gobby_home: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A session's first write reaches Git through the timer alone, with no prior ensure."""
    project_id = sample_project["id"]
    session = SessionManager(temp_db).register(
        external_id="workspace-first-write",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    folder = bind_session_workspace(temp_db, session.id)
    assert folder is not None
    assert not workspace_git_dir(project_id).exists()
    (folder / "notes.md").write_text("first finding\n")

    storage = CronJobStorage(temp_db)
    executor = _Executor()
    assert register_agent_workspace_cron(cron_storage=storage, cron_executor=executor) == 1
    (handler_name, handler), *_ = executor.handlers.items()
    (job,) = [
        job
        for job in storage.list_jobs(is_system=True)
        if job.action_config.get("handler") == handler_name
    ]
    assert await handler(job) == "agent workspaces: 1 committed, 0 failed"

    relative = folder.relative_to(workspace_anchor(project_id)).as_posix()
    assert {"README.md", f"{relative}/notes.md"} <= _tracked(project_id)
    assert _subjects(project_id) == ["agents-ws: timer default"]


@pytest.mark.parametrize("state", ["running", "disabled", "parked"])
def test_registration_migrates_interval_to_nightly_and_preserves_parked_state(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setattr("gobby.storage.cron.resolve_local_timezone", lambda zone: "America/Chicago")
    storage = CronJobStorage(temp_db)
    old = storage.create_job(
        project_id=PERSONAL_PROJECT_ID,
        name=agent_workspaces.WORKSPACE_CRON_JOB_NAME,
        schedule_type="interval",
        interval_seconds=300,
        action_type="handler",
        action_config={"handler": agent_workspaces.WORKSPACE_CRON_HANDLER},
        is_system=True,
        enabled=state != "disabled",
    )
    if state == "parked":
        parked = storage.park_system_job(old.id)
        assert parked is not None
        assert parked.enabled
        assert parked.next_run_at is None

    agent_workspaces._ensure_workspace_cron_job(storage)

    updated = storage.get_job(old.id)
    assert updated is not None
    assert updated.schedule_type == "cron"
    assert updated.cron_expr == "30 3 * * *"
    assert updated.interval_seconds is None
    assert updated.timezone == "America/Chicago"
    assert updated.enabled is (state != "disabled")
    assert (updated.next_run_at is not None) is (state == "running")
