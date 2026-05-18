"""Tests for durable mailbox delivery."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import pytest

from gobby.sessions.mailbox import MailboxSendResult, MailboxService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class WakeDispatcherProtocol(Protocol):
    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]: ...


class FakeWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


class FailingWakeDispatcher:
    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]:
        raise RuntimeError(f"wake failed for {session_id}")


def _register_session(
    session_manager: SessionManager,
    project_id: str,
    external_id: str,
    *,
    agent_depth: int = 0,
) -> Session:
    return session_manager.register(
        external_id=external_id,
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        title=external_id,
        agent_depth=agent_depth,
    )


def _mailbox(
    temp_db: LocalDatabase,
    session_manager: SessionManager,
    wake_dispatcher: WakeDispatcherProtocol | None = None,
) -> MailboxService:
    return MailboxService(
        db=temp_db,
        message_manager=InterSessionMessageManager(temp_db),
        session_manager=session_manager,
        wake_dispatcher=wake_dispatcher,
    )


def _setup_broadcast_scenario(
    temp_db: LocalDatabase,
    project_manager: LocalProjectManager,
    session_manager: SessionManager,
    project_id: str,
) -> dict[str, str]:
    agent_runs = LocalAgentRunManager(temp_db)
    ids: dict[str, str] = {}

    def register(name: str, *, agent_depth: int = 0, target_project_id: str = project_id) -> str:
        session = _register_session(
            session_manager,
            target_project_id,
            name,
            agent_depth=agent_depth,
        )
        ids[name] = session.id
        return session.id

    sender = register("sender")
    parent = register("parent")
    child_pending = register("child-pending", agent_depth=1)
    child_running = register("child-running", agent_depth=1)
    child_paused = register("child-paused", agent_depth=1)
    fallback_parent = register("fallback-parent")
    fallback_child = register("fallback-child", agent_depth=1)
    excluded_parent = register("excluded-parent")
    excluded_child = register("excluded-child", agent_depth=1)
    completed_child = register("completed-child", agent_depth=1)
    other_project_id = project_manager.create(
        name="other-project",
        repo_path="/tmp/other-project",
    ).id
    other_project = register("other-project", target_project_id=other_project_id)

    session_manager.update_status(child_paused, "paused")
    session_manager.update_status(fallback_child, "expired")
    session_manager.update_status(excluded_parent, "expired")
    session_manager.update_status(excluded_child, "expired")

    agent_runs.create(parent_session_id=parent, child_session_id=child_pending, provider="codex", prompt="pending")
    running = agent_runs.create(
        parent_session_id=parent,
        child_session_id=child_running,
        provider="codex",
        prompt="running",
    )
    agent_runs.start(running.id)
    agent_runs.create(parent_session_id=parent, child_session_id=child_paused, provider="codex", prompt="paused")
    agent_runs.create(
        parent_session_id=fallback_parent,
        child_session_id=fallback_child,
        provider="codex",
        prompt="fallback",
    )
    agent_runs.create(
        parent_session_id=excluded_parent,
        child_session_id=excluded_child,
        provider="codex",
        prompt="inactive",
    )
    completed = agent_runs.create(
        parent_session_id=parent,
        child_session_id=completed_child,
        provider="codex",
        prompt="completed",
    )
    agent_runs.complete(completed.id, "done")
    agent_runs.create(parent_session_id=sender, provider="codex", prompt="exclude sender fallback")
    agent_runs.create(parent_session_id=parent, child_session_id=child_pending, provider="codex", prompt="duplicate")
    agent_runs.create(parent_session_id=other_project, provider="codex", prompt="wrong project")
    ids["other-project-id"] = other_project_id
    return ids


async def _send_project_broadcast(
    temp_db: LocalDatabase,
    session_manager: SessionManager,
    sender_id: str,
    project_id: str,
) -> MailboxSendResult:
    return await _mailbox(temp_db, session_manager).send(
        from_session_id=sender_id,
        send_to_all=True,
        content="Broadcast",
        message_type="announcement",
        metadata={"scope": "project-agents"},
        project_id=project_id,
    )


class TestMailboxDirectSend:
    @pytest.mark.asyncio
    async def test_direct_send_creates_durable_row_and_wakes(
        self,
        temp_db: LocalDatabase,
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
        temp_db: LocalDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="to_session_id is required"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                content="No target",
            )

    @pytest.mark.asyncio
    async def test_wake_unavailable_preserves_delivery_shape(
        self,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            to_session_id=recipient.id,
            content="Wake me",
            include_wakeup=True,
        )

        assert result.wake_results == [
            {
                "session_id": recipient.id,
                "delivered": False,
                "method": None,
                "error": "wake_dispatcher_unavailable",
                "error_code": "wake_dispatcher_unavailable",
                "error_message": "Wake dispatcher is unavailable",
            }
        ]

    @pytest.mark.asyncio
    async def test_wake_exception_reports_error_code_and_message(
        self,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        result = await _mailbox(
            temp_db,
            session_manager,
            FailingWakeDispatcher(),
        ).send(
            from_session_id=sender.id,
            to_session_id=recipient.id,
            content="Wake me",
            include_wakeup=True,
        )

        assert result.wake_results == [
            {
                "session_id": recipient.id,
                "delivered": False,
                "method": None,
                "error": f"wake failed for {recipient.id}",
                "error_code": "wake_dispatch_failed",
                "error_message": f"wake failed for {recipient.id}",
            }
        ]


class TestMailboxBroadcast:
    @pytest.mark.asyncio
    async def test_send_to_all_with_no_recipients_logs_empty_broadcast(
        self,
        temp_db: LocalDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        caplog.set_level(logging.INFO, logger="gobby.sessions.mailbox")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            send_to_all=True,
            content="Broadcast",
            project_id=sample_project["id"],
        )

        assert result.recipient_session_ids == []
        assert result.message_ids == []
        assert result.broadcast_id
        assert result.to_dict()["success"] is True
        assert result.to_dict()["failed_broadcasts"] == []
        assert temp_db.fetchone("SELECT id FROM inter_session_messages LIMIT 1") is None

        log_record = next(
            record
            for record in caplog.records
            if record.message == "Mailbox broadcast resolved no recipients"
        )
        assert log_record.from_session_id == sender.id
        assert log_record.resolved_project_id == sample_project["id"]
        assert log_record.broadcast_id == result.broadcast_id

    @pytest.mark.asyncio
    async def test_send_to_all_targets_active_agent_run_sessions(
        self,
        temp_db: LocalDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
            sample_project["id"],
        )

        assert ids["child-pending"] in result.recipient_session_ids
        assert ids["child-running"] in result.recipient_session_ids
        assert ids["child-paused"] in result.recipient_session_ids
        assert result.wake_results == []
        assert result.broadcast_id
        assert len(result.message_ids) == len(result.recipient_session_ids)

    @pytest.mark.asyncio
    async def test_send_to_all_uses_active_parent_when_child_session_expired(
        self,
        temp_db: LocalDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
            sample_project["id"],
        )

        assert ids["fallback-parent"] in result.recipient_session_ids
        assert ids["fallback-child"] not in result.recipient_session_ids

    @pytest.mark.asyncio
    async def test_send_to_all_enforces_project_scope_and_sender_exclusion(
        self,
        temp_db: LocalDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
            sample_project["id"],
        )

        assert ids["other-project"] not in result.recipient_session_ids
        assert ids["sender"] not in result.recipient_session_ids
        assert ids["excluded-parent"] not in result.recipient_session_ids
        assert ids["excluded-child"] not in result.recipient_session_ids

    @pytest.mark.asyncio
    async def test_send_to_all_writes_broadcast_metadata(
        self,
        temp_db: LocalDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
            sample_project["id"],
        )

        rows = temp_db.fetchall(
            "SELECT * FROM inter_session_messages WHERE message_type = 'announcement'"
        )
        assert len(rows) == len(result.recipient_session_ids)
        metadata_payloads = [json.loads(row["metadata_json"]) for row in rows]
        assert {payload["broadcast_id"] for payload in metadata_payloads} == {
            result.broadcast_id
        }
        for payload in metadata_payloads:
            assert payload["scope"] == "project-agents"
            assert payload["broadcast"]["send_to_all"] is True
            assert payload["broadcast"]["selector"] == {
                "project_id": sample_project["id"],
                "agent_run_status": ["pending", "running"],
                "session_status": ["active", "paused"],
                "exclude_session_id": ids["sender"],
            }

    @pytest.mark.asyncio
    async def test_send_to_all_rejects_explicit_recipient(
        self,
        temp_db: LocalDatabase,
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
