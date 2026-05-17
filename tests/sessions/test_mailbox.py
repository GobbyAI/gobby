"""Tests for durable mailbox delivery."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.sessions.mailbox import MailboxService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class FakeWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


def _register_session(
    session_manager: SessionManager,
    project_id: str,
    external_id: str,
    *,
    agent_depth: int = 0,
) -> Any:
    return session_manager.register(
        external_id=external_id,
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        title=external_id,
        agent_depth=agent_depth,
    )


def _mailbox(
    temp_db: Any,
    session_manager: SessionManager,
    wake_dispatcher: Any | None = None,
) -> MailboxService:
    return MailboxService(
        db=temp_db,
        message_manager=InterSessionMessageManager(temp_db),
        session_manager=session_manager,
        wake_dispatcher=wake_dispatcher,
    )


class TestMailboxDirectSend:
    @pytest.mark.asyncio
    async def test_direct_send_creates_durable_row_and_wakes(
        self,
        temp_db: Any,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")
        wake_dispatcher = FakeWakeDispatcher()

        result = await _mailbox(temp_db, session_manager, wake_dispatcher).send(
            from_session_id=sender.id,
            to_session_id=recipient.id,
            content="  Assigned task  ",
            priority="high",
            message_type="task_assignment",
            metadata={"task_id": "#14760"},
            include_wakeup=True,
        )

        assert result.recipient_session_ids == [recipient.id]
        assert result.broadcast_id is None
        assert len(result.message_ids) == 1
        assert result.wake_results == [
            {"session_id": recipient.id, "delivered": True, "method": "fake"}
        ]
        assert wake_dispatcher.calls == [recipient.id]

        row = temp_db.fetchone(
            "SELECT * FROM inter_session_messages WHERE id = ?",
            (result.message_ids[0],),
        )
        assert row is not None
        assert row["from_session"] == sender.id
        assert row["to_session"] == recipient.id
        assert row["content"] == "Assigned task"
        assert row["priority"] == "high"
        assert row["message_type"] == "task_assignment"
        assert json.loads(row["metadata_json"]) == {"task_id": "#14760"}

    @pytest.mark.asyncio
    async def test_direct_send_requires_recipient(
        self,
        temp_db: Any,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="to_session_id is required"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                content="No target",
            )


class TestMailboxBroadcast:
    @pytest.mark.asyncio
    async def test_send_to_all_fans_out_to_active_project_agent_sessions(
        self,
        temp_db: Any,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        agent_runs = LocalAgentRunManager(temp_db)
        sender = _register_session(session_manager, sample_project["id"], "sender")
        parent = _register_session(session_manager, sample_project["id"], "parent")
        child_pending = _register_session(
            session_manager,
            sample_project["id"],
            "child-pending",
            agent_depth=1,
        )
        child_running = _register_session(
            session_manager,
            sample_project["id"],
            "child-running",
            agent_depth=1,
        )
        child_paused = _register_session(
            session_manager,
            sample_project["id"],
            "child-paused",
            agent_depth=1,
        )
        fallback_parent = _register_session(
            session_manager,
            sample_project["id"],
            "fallback-parent",
        )
        fallback_child = _register_session(
            session_manager,
            sample_project["id"],
            "fallback-child",
            agent_depth=1,
        )
        excluded_parent = _register_session(
            session_manager,
            sample_project["id"],
            "excluded-parent",
        )
        excluded_child = _register_session(
            session_manager,
            sample_project["id"],
            "excluded-child",
            agent_depth=1,
        )
        completed_child = _register_session(
            session_manager,
            sample_project["id"],
            "completed-child",
            agent_depth=1,
        )
        other_project_id = project_manager.create(
            name="other-project",
            repo_path="/tmp/other-project",
        ).id
        other_project = _register_session(session_manager, other_project_id, "other-project")

        session_manager.update_status(child_paused.id, "paused")
        session_manager.update_status(fallback_child.id, "expired")
        session_manager.update_status(excluded_parent.id, "expired")
        session_manager.update_status(excluded_child.id, "expired")

        agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=child_pending.id,
            provider="codex",
            prompt="pending",
        )
        running = agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=child_running.id,
            provider="codex",
            prompt="running",
        )
        agent_runs.start(running.id)
        agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=child_paused.id,
            provider="codex",
            prompt="paused",
        )
        agent_runs.create(
            parent_session_id=fallback_parent.id,
            child_session_id=fallback_child.id,
            provider="codex",
            prompt="fallback",
        )
        agent_runs.create(
            parent_session_id=excluded_parent.id,
            child_session_id=excluded_child.id,
            provider="codex",
            prompt="inactive",
        )
        completed = agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=completed_child.id,
            provider="codex",
            prompt="completed",
        )
        agent_runs.complete(completed.id, "done")
        agent_runs.create(
            parent_session_id=sender.id,
            provider="codex",
            prompt="exclude sender fallback",
        )
        agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=child_pending.id,
            provider="codex",
            prompt="duplicate",
        )
        agent_runs.create(
            parent_session_id=other_project.id,
            provider="codex",
            prompt="wrong project",
        )

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            send_to_all=True,
            content="Broadcast",
            message_type="announcement",
            metadata={"scope": "project-agents"},
            project_id=sample_project["id"],
        )

        assert result.recipient_session_ids == [
            child_pending.id,
            child_running.id,
            child_paused.id,
            fallback_parent.id,
        ]
        assert result.wake_results == []
        assert result.broadcast_id
        assert len(result.message_ids) == len(result.recipient_session_ids)

        rows = temp_db.fetchall(
            "SELECT * FROM inter_session_messages WHERE message_type = 'announcement'"
        )
        assert len(rows) == len(result.recipient_session_ids)
        metadata_payloads = [json.loads(row["metadata_json"]) for row in rows]
        assert {payload["broadcast_id"] for payload in metadata_payloads} == {result.broadcast_id}
        for payload in metadata_payloads:
            assert payload["scope"] == "project-agents"
            assert payload["broadcast"]["send_to_all"] is True
            assert payload["broadcast"]["selector"] == {
                "project_id": sample_project["id"],
                "agent_run_status": ["pending", "running"],
                "session_status": ["active", "paused"],
                "exclude_session_id": sender.id,
            }

    @pytest.mark.asyncio
    async def test_send_to_all_rejects_explicit_recipient(
        self,
        temp_db: Any,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        with pytest.raises(ValueError, match="cannot be combined"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                to_session_id=recipient.id,
                send_to_all=True,
                content="Invalid",
                project_id=sample_project["id"],
            )
