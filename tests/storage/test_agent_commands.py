"""Tests for AgentCommandManager, message delivery, and session ancestry.

Covers:
- AgentCommand dataclass and AgentCommandManager CRUD
- Status transitions (pending → running → completed/failed/cancelled)
- InterSessionMessageManager.get_undelivered_messages / mark_delivered
- LocalSessionManager.is_ancestor
"""

from __future__ import annotations

import json
import uuid

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit

_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_agent_commands.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    # Ensure test project
    database.execute(
        """INSERT OR IGNORE INTO projects (id, name, repo_path, created_at, updated_at)
           VALUES (?, '_test', '/tmp/test', datetime('now'), datetime('now'))""",
        (_TEST_PROJECT_ID,),
    )
    return database


def _create_session(
    db: LocalDatabase, session_id: str | None = None, parent_id: str | None = None
) -> str:
    """Create a test session, return its ID."""
    sid = session_id or str(uuid.uuid4())
    db.execute(
        """INSERT OR IGNORE INTO sessions
           (id, external_id, machine_id, source, project_id, status, parent_session_id)
           VALUES (?, ?, 'test-machine', 'test', ?, 'active', ?)""",
        (sid, sid, _TEST_PROJECT_ID, parent_id),
    )
    return sid


# ═══════════════════════════════════════════════════════════════════════
# AgentCommand dataclass
# ═══════════════════════════════════════════════════════════════════════


class TestAgentCommandDataclass:
    """AgentCommand dataclass creation and serialization."""

    def test_create_from_fields(self) -> None:
        from gobby.storage.agent_commands import AgentCommand

        cmd = AgentCommand(
            id="cmd-1",
            from_session="s1",
            to_session="s2",
            command_text="Run tests",
            status="pending",
            created_at="2026-01-01T00:00:00",
        )
        assert cmd.id == "cmd-1"
        assert cmd.command_text == "Run tests"
        assert cmd.status == "pending"
        assert cmd.allowed_tools is None
        assert cmd.exit_condition is None

    def test_to_dict(self) -> None:
        from gobby.storage.agent_commands import AgentCommand

        cmd = AgentCommand(
            id="cmd-1",
            from_session="s1",
            to_session="s2",
            command_text="Run tests",
            allowed_tools='["Read", "Grep"]',
            status="running",
            created_at="2026-01-01T00:00:00",
            started_at="2026-01-01T00:00:01",
        )
        d = cmd.to_dict()
        assert d["id"] == "cmd-1"
        assert d["command_text"] == "Run tests"
        assert d["allowed_tools"] == '["Read", "Grep"]'
        assert d["started_at"] == "2026-01-01T00:00:01"
        assert d["completed_at"] is None


# ═══════════════════════════════════════════════════════════════════════
# AgentCommandManager CRUD
# ═══════════════════════════════════════════════════════════════════════


class TestAgentCommandManagerCRUD:
    """AgentCommandManager create, get, list, update operations."""

    def test_create_command(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        cmd = mgr.create_command(
            from_session=from_s,
            to_session=to_s,
            command_text="Run tests",
        )
        assert cmd.id is not None
        assert cmd.from_session == from_s
        assert cmd.to_session == to_s
        assert cmd.command_text == "Run tests"
        assert cmd.status == "pending"

    def test_create_with_tools(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        cmd = mgr.create_command(
            from_session=from_s,
            to_session=to_s,
            command_text="Search code",
            allowed_tools=["Read", "Grep"],
            allowed_mcp_tools=["gobby-tasks:list_tasks"],
            exit_condition="task_complete()",
        )
        assert json.loads(cmd.allowed_tools) == ["Read", "Grep"]
        assert json.loads(cmd.allowed_mcp_tools) == ["gobby-tasks:list_tasks"]
        assert cmd.exit_condition == "task_complete()"

    def test_get_command(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        created = mgr.create_command(from_s, to_s, "Test")
        fetched = mgr.get_command(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.command_text == "Test"

    def test_get_command_not_found(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        assert mgr.get_command("nonexistent") is None

    def test_list_commands_for_session(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)
        other_s = _create_session(db)

        mgr.create_command(from_s, to_s, "Task 1")
        mgr.create_command(from_s, to_s, "Task 2")
        mgr.create_command(from_s, other_s, "Task 3")

        commands = mgr.list_commands(to_session=to_s)
        assert len(commands) == 2

    def test_list_commands_by_status(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        cmd1 = mgr.create_command(from_s, to_s, "Task 1")
        mgr.create_command(from_s, to_s, "Task 2")
        mgr.update_status(cmd1.id, "running")

        pending = mgr.list_commands(to_session=to_s, status="pending")
        assert len(pending) == 1
        assert pending[0].command_text == "Task 2"


# ═══════════════════════════════════════════════════════════════════════
# AgentCommandManager status transitions
# ═══════════════════════════════════════════════════════════════════════


class TestAgentCommandStatusTransitions:
    """Status lifecycle: pending → running → completed/failed/cancelled."""

    def test_transition_to_running(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        cmd = mgr.create_command(from_s, to_s, "Work")
        updated = mgr.update_status(cmd.id, "running")
        assert updated.status == "running"
        assert updated.started_at is not None

    def test_transition_to_completed(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        cmd = mgr.create_command(from_s, to_s, "Work")
        mgr.update_status(cmd.id, "running")
        updated = mgr.update_status(cmd.id, "completed")
        assert updated.status == "completed"
        assert updated.completed_at is not None

    def test_transition_to_failed(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        cmd = mgr.create_command(from_s, to_s, "Work")
        mgr.update_status(cmd.id, "running")
        updated = mgr.update_status(cmd.id, "failed")
        assert updated.status == "failed"
        assert updated.completed_at is not None

    def test_transition_to_cancelled(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        cmd = mgr.create_command(from_s, to_s, "Work")
        updated = mgr.update_status(cmd.id, "cancelled")
        assert updated.status == "cancelled"
        assert updated.completed_at is not None

    def test_update_not_found_raises(self, db: LocalDatabase) -> None:
        from gobby.storage.agent_commands import AgentCommandManager

        mgr = AgentCommandManager(db)
        with pytest.raises(ValueError, match="not found"):
            mgr.update_status("nonexistent", "running")


# ═══════════════════════════════════════════════════════════════════════
# InterSessionMessageManager delivery
# ═══════════════════════════════════════════════════════════════════════


class TestMessageDelivery:
    """get_undelivered_messages and mark_delivered."""

    def test_get_undelivered_messages(self, db: LocalDatabase) -> None:
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        mgr = InterSessionMessageManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        mgr.create_message(from_s, to_s, "msg1")
        mgr.create_message(from_s, to_s, "msg2")

        undelivered = mgr.get_undelivered_messages(to_s)
        assert len(undelivered) == 2

    def test_mark_delivered(self, db: LocalDatabase) -> None:
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        mgr = InterSessionMessageManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        msg = mgr.create_message(from_s, to_s, "test")
        delivered = mgr.mark_delivered(msg.id)
        assert delivered.delivered_at is not None

    def test_delivered_excluded_from_undelivered(self, db: LocalDatabase) -> None:
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        mgr = InterSessionMessageManager(db)
        from_s = _create_session(db)
        to_s = _create_session(db)

        msg1 = mgr.create_message(from_s, to_s, "msg1")
        mgr.create_message(from_s, to_s, "msg2")

        mgr.mark_delivered(msg1.id)

        undelivered = mgr.get_undelivered_messages(to_s)
        assert len(undelivered) == 1
        assert undelivered[0].content == "msg2"

    def test_mark_delivered_not_found(self, db: LocalDatabase) -> None:
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        mgr = InterSessionMessageManager(db)
        with pytest.raises(ValueError, match="not found"):
            mgr.mark_delivered("nonexistent")


# ═══════════════════════════════════════════════════════════════════════
# Session ancestry
# ═══════════════════════════════════════════════════════════════════════


class TestSessionAncestry:
    """is_ancestor checks parent-child chain."""

    def test_direct_parent(self, db: LocalDatabase) -> None:
        from gobby.storage.sessions import LocalSessionManager

        mgr = LocalSessionManager(db)
        parent = _create_session(db)
        child = _create_session(db, parent_id=parent)

        assert mgr.is_ancestor(ancestor_id=parent, descendant_id=child) is True

    def test_grandparent(self, db: LocalDatabase) -> None:
        from gobby.storage.sessions import LocalSessionManager

        mgr = LocalSessionManager(db)
        grandparent = _create_session(db)
        parent = _create_session(db, parent_id=grandparent)
        child = _create_session(db, parent_id=parent)

        assert mgr.is_ancestor(ancestor_id=grandparent, descendant_id=child) is True

    def test_not_ancestor(self, db: LocalDatabase) -> None:
        from gobby.storage.sessions import LocalSessionManager

        mgr = LocalSessionManager(db)
        s1 = _create_session(db)
        s2 = _create_session(db)

        assert mgr.is_ancestor(ancestor_id=s1, descendant_id=s2) is False

    def test_self_is_not_ancestor(self, db: LocalDatabase) -> None:
        from gobby.storage.sessions import LocalSessionManager

        mgr = LocalSessionManager(db)
        s = _create_session(db)

        assert mgr.is_ancestor(ancestor_id=s, descendant_id=s) is False

    def test_reverse_direction(self, db: LocalDatabase) -> None:
        from gobby.storage.sessions import LocalSessionManager

        mgr = LocalSessionManager(db)
        parent = _create_session(db)
        child = _create_session(db, parent_id=parent)

        assert mgr.is_ancestor(ancestor_id=child, descendant_id=parent) is False
