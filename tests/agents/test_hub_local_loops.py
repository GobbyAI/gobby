"""Hub machine-local loops leave other machines' rows alone."""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.agents.spawn_executor import reap_stale_pending_terminals
from gobby.agents.terminal_cleanup import TerminalResourceCleaner
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.clones import CloneStatus, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from gobby.utils.machine_id import require_machine_id
from tests.agents.cleanup_test_support import _handler, _run
from tests.agents.terminal_fixtures import make_pending_terminal
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
REMOTE_MACHINE_ID = "21000000-0000-4000-8000-000000000099"


@pytest.fixture(autouse=True)
def _pin_local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


def _pending(manager: TerminalManager, project_id: str, machine_id: str) -> str:
    terminal_id = str(uuid.uuid4())
    row = manager.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="tmux",
        ownership="gobby",
        spawn_key=f"gobby-{terminal_id}",
        machine_id=machine_id,
    )
    return row.id


@pytest.mark.asyncio
async def test_reaper_skips_remote_pending(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """The stale-pending reaper reaps a local row and leaves a remote one."""
    manager = TerminalManager(temp_db)
    LocalMachineManager(temp_db).upsert_seen(REMOTE_MACHINE_ID, TEST_USER_ID)
    local_id = _pending(manager, sample_project["id"], require_machine_id())
    remote_id = _pending(manager, sample_project["id"], REMOTE_MACHINE_ID)
    seen: list[str] = []

    async def fake_reap(
        _manager: TerminalManager,
        _registry: object,
        listed: Any,
    ) -> bool:
        seen.append(listed.id)
        return True

    with patch("gobby.agents.spawn_executor._reap_stale_row", fake_reap):
        reaped = await reap_stale_pending_terminals(
            manager,
            MagicMock(),
            in_doubt_seconds=0,
        )

    assert reaped == [local_id]
    assert seen == [local_id]
    assert remote_id not in seen


def _terminal_run(
    manager: LocalAgentRunManager,
    db: HubDatabase,
    session_id: str,
    prompt: str,
) -> str:
    run = manager.create(parent_session_id=session_id, provider="claude", prompt=prompt)
    make_pending_terminal(run, backend="tmux", db=db)
    db.execute(
        """
        UPDATE agent_runs
        SET status = 'success', completed_at = now(), updated_at = now()
        WHERE id = %s
        """,
        (run.id,),
    )
    return run.id


@pytest.mark.asyncio
async def test_tmux_cleanup_is_machine_scoped(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    """Tmux cleanup closes this machine's terminal runs and skips a remote one."""
    LocalMachineManager(temp_db).upsert_seen(REMOTE_MACHINE_ID, TEST_USER_ID)
    session = session_manager.register(
        external_id=f"hub-local-tmux-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)
    local_id = _terminal_run(manager, temp_db, session.id, "local")
    remote_id = _terminal_run(manager, temp_db, session.id, "remote")
    temp_db.execute(
        "UPDATE agent_runs SET machine_id = %s WHERE id = %s",
        (REMOTE_MACHINE_ID, remote_id),
    )
    closed: list[str] = []

    async def fake_close(_self: TerminalResourceCleaner, run: Any) -> bool:
        closed.append(run.id)
        return True

    handler = _handler(temp_db, agent_run_manager=manager)
    with patch.object(TerminalResourceCleaner, "_close_tmux_session", fake_close):
        await handler.cleanup_terminal_tmux_sessions()

    assert local_id in closed
    assert remote_id not in closed


@pytest.mark.asyncio
async def test_post_cleanup_skips_local_phases_for_remote_run(
    temp_db: HubDatabase,
) -> None:
    """A remote run still delivers, attends, and releases ownership."""
    run = replace(_run(), machine_id=REMOTE_MACHINE_ID)
    handler = _handler(temp_db)
    attention = AsyncMock()
    handler._resource_cleaner._attention_manager = attention
    delivery: list[str] = []
    tmux: list[str] = []
    srt: list[str] = []
    sandbox: list[str] = []
    ownership: list[str] = []

    async def fake_delivery(**kwargs: Any) -> None:
        delivery.append(str(kwargs["run_id"]))

    async def fake_tmux(_self: TerminalResourceCleaner, listed: Any) -> bool:
        tmux.append(listed.id)
        return True

    async def fake_srt(run_id: str) -> None:
        srt.append(run_id)

    async def fake_sandbox(run_id: str) -> Any:
        sandbox.append(run_id)
        return SimpleNamespace(retention_metadata=lambda: {})

    def fake_ownership(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        ownership.append(str(kwargs["run_id"]))
        return SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0)

    with (
        patch(
            "gobby.agents.terminal_cleanup.terminal_delivery.deliver_and_cleanup_terminal_run",
            fake_delivery,
        ),
        patch.object(TerminalResourceCleaner, "_close_tmux_session", fake_tmux),
        patch("gobby.agents.terminal_cleanup.reap_srt_runner_process_tree", fake_srt),
        patch("gobby.agents.terminal_cleanup.reap_sandbox_run_roots", fake_sandbox),
        patch(
            "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
            fake_ownership,
        ),
    ):
        await handler.post_terminal_cleanup(run)

    assert tmux == []
    assert srt == []
    assert sandbox == []
    assert delivery
    assert attention.transition_async.await_count == 1
    assert ownership == [run.id]


@pytest.mark.asyncio
async def test_interactive_monitor_is_machine_scoped() -> None:
    """The monitor lists only this machine's interactive sessions."""
    remote = SimpleNamespace(id="remote-session")
    calls: list[dict[str, Any]] = []

    def fake_list(**kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        if kwargs.get("machine_id") == require_machine_id():
            return []
        return [remote]

    monitor = InteractiveAttentionMonitor(
        MagicMock(),
        session_manager=cast(SessionManager, SimpleNamespace(list=fake_list)),
        prompt_detector=MagicMock(),
        stall_classifier=MagicMock(),
        registry=MagicMock(),
    )
    found = await monitor._list_interactive_sessions()
    assert len(calls) == 1
    assert calls[0]["machine_id"] == require_machine_id()
    assert calls[0]["modes"] == ["interactive"]
    assert [session.id for session in found] == []


def _mark_expired(db: HubDatabase, table: str, row_id: str, machine_id: str) -> None:
    status = WorktreeStatus.MERGED.value if table == "worktrees" else CloneStatus.MERGED.value
    db.execute(
        f"""
        UPDATE {table}
        SET status = %s,
            cleanup_after = now() - interval '1 hour',
            agent_session_id = NULL,
            machine_id = %s
        WHERE id = %s
        """,
        (status, machine_id, row_id),
    )


def _move_owned_worktree(
    db: HubDatabase, worktree_id: str, session_id: str, machine_id: str
) -> None:
    """Move one owned worktree onto another machine.

    sessions.id is the primary key, and worktrees reference (id, machine_id),
    so the owner row has to move with the worktree inside one deferred check.
    """
    with db.transaction() as conn:
        conn.execute("SET CONSTRAINTS worktrees_agent_session_id_machine_id_fkey DEFERRED")
        conn.execute(
            "UPDATE sessions SET machine_id = %s WHERE id = %s",
            (machine_id, session_id),
        )
        conn.execute(
            "UPDATE worktrees SET machine_id = %s WHERE id = %s",
            (machine_id, worktree_id),
        )


def test_workspace_loops_skip_remote_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    """Expired cleanup and session release stay on this machine's rows."""
    LocalMachineManager(temp_db).upsert_seen(REMOTE_MACHINE_ID, TEST_USER_ID)
    project_id = sample_project["id"]
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)
    local_worktree = worktrees.create(
        project_id,
        "local-expired",
        f"/worktrees/{uuid.uuid4()}",
    )
    remote_worktree = worktrees.create(
        project_id,
        "remote-expired",
        f"/worktrees/{uuid.uuid4()}",
    )
    local_clone = clones.create(project_id, "local-expired", f"/clones/{uuid.uuid4()}")
    remote_clone = clones.create(project_id, "remote-expired", f"/clones/{uuid.uuid4()}")
    _mark_expired(temp_db, "worktrees", local_worktree.id, require_machine_id())
    _mark_expired(temp_db, "worktrees", remote_worktree.id, REMOTE_MACHINE_ID)
    _mark_expired(temp_db, "clones", local_clone.id, require_machine_id())
    _mark_expired(temp_db, "clones", remote_clone.id, REMOTE_MACHINE_ID)

    expired_worktrees = {row.id for row in worktrees.find_expired(project_id=project_id)}
    expired_clones = {row.id for row in clones.find_expired(project_id=project_id)}
    assert local_worktree.id in expired_worktrees
    assert remote_worktree.id not in expired_worktrees
    assert local_clone.id in expired_clones
    assert remote_clone.id not in expired_clones

    listed_worktrees = {
        row.id for row in worktrees.list_worktrees(project_id=project_id, limit=None)
    }
    listed_clones = {row.id for row in clones.list_clones(project_id=project_id, limit=100)}
    assert local_worktree.id in listed_worktrees
    assert remote_worktree.id not in listed_worktrees
    assert local_clone.id in listed_clones
    assert remote_clone.id not in listed_clones
    temp_db.execute(
        "UPDATE clones SET status = %s WHERE id IN (%s, %s)",
        (CloneStatus.CLEANUP.value, local_clone.id, remote_clone.id),
    )
    pending_clones = {row.id for row in clones.list_cleanup_pending()}
    assert local_clone.id in pending_clones
    assert remote_clone.id not in pending_clones

    session = session_manager.register(
        external_id=f"hub-local-release-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    owned_local = worktrees.create(
        project_id,
        "local-owned",
        f"/worktrees/{uuid.uuid4()}",
        agent_session_id=session.id,
    )
    SessionCoordinator(worktree_manager=worktrees).release_session_worktrees(session.id)
    owned_remote = worktrees.create(
        project_id,
        "remote-owned",
        f"/worktrees/{uuid.uuid4()}",
        agent_session_id=session.id,
    )
    _move_owned_worktree(temp_db, owned_remote.id, session.id, REMOTE_MACHINE_ID)
    SessionCoordinator(worktree_manager=worktrees).release_session_worktrees(session.id)

    local_owner = temp_db.fetchone(
        "SELECT agent_session_id FROM worktrees WHERE id = %s",
        (owned_local.id,),
    )
    remote_owner = temp_db.fetchone(
        "SELECT machine_id, agent_session_id FROM worktrees WHERE id = %s",
        (owned_remote.id,),
    )
    remote_row = temp_db.fetchone(
        "SELECT machine_id, status, agent_session_id FROM worktrees WHERE id = %s",
        (remote_worktree.id,),
    )
    assert local_owner is not None
    assert remote_owner is not None
    assert remote_row is not None
    assert local_owner["agent_session_id"] is None
    assert remote_owner["machine_id"] == REMOTE_MACHINE_ID
    assert remote_owner["agent_session_id"] == session.id
    assert remote_row["machine_id"] == REMOTE_MACHINE_ID
    assert remote_row["status"] == WorktreeStatus.MERGED.value
    assert remote_row["agent_session_id"] is None
