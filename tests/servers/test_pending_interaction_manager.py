"""Tests for PendingInteractionManager — durable approval state coordination.

Covers: create, wait, resolve, expire, supersede, rebroadcast, cleanup,
expire_all_pending.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.servers.pending_interactions import PendingInteractionManager
from gobby.storage.hub.protocol import HubDatabase
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


# projects.id, sessions.id, and pending_interactions.session_id are native
# uuid columns; ids must be valid UUID strings.
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
SESSION_1 = "22222222-2222-4222-8222-222222222221"
SESSION_2 = "22222222-2222-4222-8222-222222222222"
SESSION_IDS = [SESSION_1, SESSION_2]


@pytest.fixture
def db(hub_db):
    """Create a fresh test database with pending_interactions table and stub sessions."""
    # Insert stub project + session rows to satisfy FK constraints
    with hub_db.transaction() as conn:
        conn.execute("INSERT INTO projects (id, name) VALUES (%s, 'test')", (PROJECT_ID,))
        for sid in SESSION_IDS:
            conn.execute(
                """INSERT INTO sessions (id, external_id, machine_id, source, project_id, session_type)
                   VALUES (%s, %s, 'test-machine', 'claude', %s, 'web_chat')""",
                (sid, sid, PROJECT_ID),
            )
    return hub_db


@pytest.fixture
def manager(db: HubDatabase) -> PendingInteractionManager:
    return PendingInteractionManager(db)


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_interaction_id(self, manager: PendingInteractionManager) -> None:
        iid = await manager.create(
            session_id=SESSION_1,
            kind="tool",
            provider="claude",
            payload={"tool_name": "Read"},
        )
        assert isinstance(iid, str)
        assert len(iid) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_create_inserts_db_row(
        self, manager: PendingInteractionManager, db: HubDatabase
    ) -> None:
        iid = await manager.create(
            session_id=SESSION_1,
            kind="tool",
            provider="claude",
            payload={"tool_name": "Read"},
            tool_name="Read",
        )
        row = db.fetchone("SELECT * FROM pending_interactions WHERE id = %s", (iid,))
        assert row is not None
        assert row["session_id"] == SESSION_1
        assert row["kind"] == "tool"
        assert row["status"] == "pending"
        assert row["tool_name"] == "Read"


class TestResolve:
    @pytest.mark.asyncio
    async def test_resolve_updates_db(
        self, manager: PendingInteractionManager, db: HubDatabase
    ) -> None:
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        result = await manager.resolve(iid, "approve")
        assert result is True
        row = db.fetchone("SELECT * FROM pending_interactions WHERE id = %s", (iid,))
        assert row["status"] == "resolved"
        assert row["decision"] == "approve"

    @pytest.mark.asyncio
    async def test_resolve_wakes_waiter(self, manager: PendingInteractionManager) -> None:
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})

        task = asyncio.create_task(manager.wait(iid))
        await drain_asyncio_tasks()
        await manager.resolve(iid, "approve")
        result = await task
        assert result["decision"] == "approve"

    @pytest.mark.asyncio
    async def test_resolve_expired_returns_false(self, manager: PendingInteractionManager) -> None:
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.expire(iid)
        result = await manager.resolve(iid, "approve")
        assert result is False


class TestExpire:
    @pytest.mark.asyncio
    async def test_expire_marks_timeout(
        self, manager: PendingInteractionManager, db: HubDatabase
    ) -> None:
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.expire(iid)
        row = db.fetchone("SELECT * FROM pending_interactions WHERE id = %s", (iid,))
        assert row["status"] == "expired"
        assert row["decision"] == "timeout"

    @pytest.mark.asyncio
    async def test_expire_wakes_waiter_with_timeout(
        self, manager: PendingInteractionManager
    ) -> None:
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})

        task = asyncio.create_task(manager.wait(iid))
        await drain_asyncio_tasks()
        await manager.expire(iid)
        result = await task
        assert result["decision"] == "timeout"

    async def test_resolve_wins_race_before_expire_wakes_waiter(self, db: HubDatabase) -> None:
        resolve_db_written = asyncio.Event()
        release_resolve = asyncio.Event()

        async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            if func.__name__ == "_resolve_pending":
                resolve_db_written.set()
                await release_resolve.wait()
            return result

        manager = PendingInteractionManager(db, run_db=run_db)
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        waiter = asyncio.create_task(manager.wait(iid))
        resolve_task = asyncio.create_task(manager.resolve(iid, "approve"))

        await resolve_db_written.wait()
        await manager.expire(iid)
        release_resolve.set()

        assert await resolve_task is True
        assert (await waiter)["decision"] == "approve"
        row = db.fetchone("SELECT status, decision FROM pending_interactions WHERE id = %s", (iid,))
        assert row == {"status": "resolved", "decision": "approve"}


class TestSupersede:
    @pytest.mark.asyncio
    async def test_supersede_expires_existing(
        self, manager: PendingInteractionManager, db: HubDatabase
    ) -> None:
        iid1 = await manager.create(
            session_id=SESSION_1, kind="tool", provider="claude", payload={"n": 1}
        )
        _iid2 = await manager.create(
            session_id=SESSION_1, kind="tool", provider="claude", payload={"n": 2}
        )
        row = db.fetchone("SELECT * FROM pending_interactions WHERE id = %s", (iid1,))
        assert row["status"] == "expired"


class TestRebroadcast:
    @pytest.mark.asyncio
    async def test_rebroadcast_returns_pending(self, manager: PendingInteractionManager) -> None:
        await manager.create(
            session_id=SESSION_1,
            kind="tool",
            provider="claude",
            payload={"tool_name": "Read"},
            tool_name="Read",
        )
        result = await manager.rebroadcast(SESSION_1)
        assert len(result) == 1
        assert result[0]["kind"] == "tool"
        assert result[0]["tool_name"] == "Read"

    @pytest.mark.asyncio
    async def test_rebroadcast_excludes_resolved(self, manager: PendingInteractionManager) -> None:
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.resolve(iid, "approve")
        result = await manager.rebroadcast(SESSION_1)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_rebroadcast_only_latest_per_kind(
        self, manager: PendingInteractionManager
    ) -> None:
        # Create tool + plan interactions
        await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={"n": 1})
        await manager.create(session_id=SESSION_1, kind="plan", provider="claude", payload={"n": 2})
        result = await manager.rebroadcast(SESSION_1)
        kinds = {r["kind"] for r in result}
        assert kinds == {"tool", "plan"}


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_wakes_existing_waiter_without_database(self) -> None:
        """Cleanup denies an in-memory waiter even when DB cleanup is unavailable."""
        manager = PendingInteractionManager(MagicMock())
        manager._waiters["interaction-1"] = asyncio.get_running_loop().create_future()

        task = asyncio.create_task(manager.wait("interaction-1"))
        await drain_asyncio_tasks()
        await manager.cleanup()

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == {"decision": "deny", "reason": "daemon_shutdown"}
        assert len(manager._waiters) == 0
        assert len(manager._timeouts) == 0

    @pytest.mark.asyncio
    async def test_cleanup_clears_state(self, manager: PendingInteractionManager) -> None:
        """Cleanup should clear waiter and timeout registries."""
        await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.cleanup()
        assert len(manager._waiters) == 0
        assert len(manager._timeouts) == 0

    @pytest.mark.asyncio
    async def test_cleanup_wakes_waiter_with_shutdown_denial(
        self, manager: PendingInteractionManager
    ) -> None:
        """Cleanup should wake active waiters with an explicit shutdown denial."""
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})

        task = asyncio.create_task(manager.wait(iid))
        await drain_asyncio_tasks()
        await manager.cleanup()

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == {"decision": "deny", "reason": "daemon_shutdown"}
        assert len(manager._waiters) == 0
        assert len(manager._timeouts) == 0


class TestExpireAllPending:
    @pytest.mark.asyncio
    async def test_expire_all_marks_expired(
        self, manager: PendingInteractionManager, db: HubDatabase
    ) -> None:
        await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.create(session_id=SESSION_2, kind="plan", provider="claude", payload={})
        await manager.expire_all_pending()
        rows = db.fetchall("SELECT status FROM pending_interactions WHERE status = 'pending'")
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_expire_all_clears_in_memory(self, manager: PendingInteractionManager) -> None:
        await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.expire_all_pending()
        assert len(manager._waiters) == 0
        assert len(manager._timeouts) == 0


class TestCountPending:
    @pytest.mark.asyncio
    async def test_count_pending(self, manager: PendingInteractionManager) -> None:
        await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.create(session_id=SESSION_1, kind="plan", provider="claude", payload={})
        count = await manager.count_pending(SESSION_1)
        assert count == 2

    @pytest.mark.asyncio
    async def test_count_pending_excludes_resolved(
        self, manager: PendingInteractionManager
    ) -> None:
        iid = await manager.create(session_id=SESSION_1, kind="tool", provider="claude", payload={})
        await manager.resolve(iid, "approve")
        count = await manager.count_pending(SESSION_1)
        assert count == 0
