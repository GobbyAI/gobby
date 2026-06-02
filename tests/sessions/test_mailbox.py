"""Tests for durable mailbox delivery."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import pytest

import gobby.sessions.mailbox as mailbox_module
from gobby.sessions.mailbox import MailboxSendResult, MailboxService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.build_history import BuildHistoryStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SYSTEM_SESSION_ID, SessionManager
from gobby.storage.tasks import LocalTaskManager

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
    temp_db: HubDatabase,
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
    temp_db: HubDatabase,
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

    agent_runs.create(
        parent_session_id=parent, child_session_id=child_pending, provider="codex", prompt="pending"
    )
    running = agent_runs.create(
        parent_session_id=parent,
        child_session_id=child_running,
        provider="codex",
        prompt="running",
    )
    agent_runs.start(running.id)
    agent_runs.create(
        parent_session_id=parent, child_session_id=child_paused, provider="codex", prompt="paused"
    )
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
    agent_runs.create(
        parent_session_id=parent,
        child_session_id=child_pending,
        provider="codex",
        prompt="duplicate",
    )
    agent_runs.create(parent_session_id=other_project, provider="codex", prompt="wrong project")
    ids["other-project-id"] = other_project_id
    return ids


async def _send_project_broadcast(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sender_id: str,
    project_id: str,
) -> MailboxSendResult:
    return await _mailbox(temp_db, session_manager).send(
        from_session_id=sender_id,
        target="project",
        target_id=project_id,
        content="Broadcast",
        message_type="announcement",
        metadata={"scope": "project-agents"},
    )


class TestMailboxDirectSend:
    @pytest.mark.asyncio
    async def test_direct_send_creates_durable_row_and_wakes(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")
        wake_dispatcher = FakeWakeDispatcher()

        result = await _mailbox(temp_db, session_manager, wake_dispatcher).send(
            from_session_id=sender.id,
            target="session",
            target_id=recipient.id,
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
            "SELECT * FROM inter_session_messages WHERE id = %s",
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
    async def test_system_session_direct_send_uses_explicit_project_scope(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=SYSTEM_SESSION_ID,
            target="session",
            target_id=recipient.id,
            project_id=sample_project["id"],
            content="System notice",
        )

        assert result.recipient_session_ids == [recipient.id]
        row = temp_db.fetchone(
            "SELECT from_session, to_session, content FROM inter_session_messages"
        )
        assert row == {
            "from_session": SYSTEM_SESSION_ID,
            "to_session": recipient.id,
            "content": "System notice",
        }

    def test_empty_project_id_is_resolved_explicitly(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mailbox = _mailbox(temp_db, session_manager)
        seen_refs: list[str] = []

        def resolve_project_ref(project_ref: str) -> str:
            seen_refs.append(project_ref)
            return "resolved-project"

        monkeypatch.setattr(mailbox, "_resolve_project_ref", resolve_project_ref)

        assert mailbox._resolve_project_id(SYSTEM_SESSION_ID, "") == "resolved-project"
        assert seen_refs == [""]

    @pytest.mark.asyncio
    async def test_session_target_requires_target_id(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="target_id is required"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="session",
                content="No target",
            )

    @pytest.mark.asyncio
    async def test_wake_unavailable_preserves_delivery_shape(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="session",
            target_id=recipient.id,
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
        temp_db: HubDatabase,
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
            target="session",
            target_id=recipient.id,
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
    async def test_project_target_with_no_recipients_logs_empty_fanout(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        caplog.set_level(logging.INFO, logger="gobby.sessions.mailbox")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="project",
            target_id=sample_project["id"],
            content="Broadcast",
        )

        assert result.recipient_session_ids == []
        assert result.message_ids == []
        assert result.broadcast_id
        assert result.target == "project"
        assert result.target_id == sample_project["id"]
        assert result.to_dict()["success"] is True
        assert result.to_dict()["failed_broadcasts"] == []
        assert temp_db.fetchone("SELECT id FROM inter_session_messages LIMIT 1") is None

        log_record = next(
            record
            for record in caplog.records
            if record.message == "Mailbox target resolved no recipients"
        )
        assert log_record.from_session_id == sender.id
        assert log_record.target == "project"
        assert log_record.target_id == sample_project["id"]
        assert log_record.broadcast_id == result.broadcast_id

    @pytest.mark.asyncio
    async def test_project_target_fans_out_to_active_agent_run_sessions(
        self,
        temp_db: HubDatabase,
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
    async def test_project_target_uses_active_parent_when_child_session_expired(
        self,
        temp_db: HubDatabase,
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
    async def test_project_target_enforces_project_scope_and_sender_exclusion(
        self,
        temp_db: HubDatabase,
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
    async def test_project_target_writes_selector_metadata(
        self,
        temp_db: HubDatabase,
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
        assert {payload["broadcast_id"] for payload in metadata_payloads} == {result.broadcast_id}
        for payload in metadata_payloads:
            assert payload["scope"] == "project-agents"
            assert payload["broadcast"]["target"] == "project"
            assert payload["broadcast"]["target_id"] == sample_project["id"]
            assert payload["broadcast"]["selector"] == {
                "target": "project",
                "project_id": sample_project["id"],
                "agent_run_status": ["pending", "running"],
                "session_status": ["active", "paused"],
                "exclude_session_id": ids["sender"],
            }

    @pytest.mark.asyncio
    async def test_all_target_rejects_target_id(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="target_id is not allowed"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="all",
                target_id=sample_project["id"],
                content="Invalid",
            )

    @pytest.mark.asyncio
    async def test_all_target_reaches_every_deliverable_non_system_session(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        active = _register_session(session_manager, sample_project["id"], "active")
        paused = _register_session(session_manager, sample_project["id"], "paused")
        expired = _register_session(session_manager, sample_project["id"], "expired")
        session_manager.update_status(paused.id, "paused")
        session_manager.update_status(expired.id, "expired")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="all",
            content="Global notice",
        )

        assert result.recipient_session_ids == [active.id, paused.id]
        assert result.broadcast_id
        assert result.selector_metadata == {
            "target": "all",
            "session_status": ["active", "paused"],
            "exclude_session_id": sender.id,
            "exclude_system_session": True,
        }

    @pytest.mark.asyncio
    async def test_agent_target_resolves_active_agent_run_recipient(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        agent_runs = LocalAgentRunManager(temp_db)
        sender = _register_session(session_manager, sample_project["id"], "sender")
        parent = _register_session(session_manager, sample_project["id"], "parent")
        child = _register_session(session_manager, sample_project["id"], "child", agent_depth=1)
        run = agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=child.id,
            provider="codex",
            prompt="work",
        )

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="agent",
            target_id=run.id,
            content="Status?",
        )

        assert result.recipient_session_ids == [child.id]
        assert result.broadcast_id is None
        assert result.selector_metadata == {
            "target": "agent",
            "agent_run_id": run.id,
            "agent_run_status": "pending",
            "task_id": None,
        }

    def test_agent_cross_project_auth_cache_uses_ttl_and_skips_missing_task(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mailbox = _mailbox(temp_db, session_manager)
        project = LocalProjectManager(temp_db).create(name="sender-project", repo_path="/tmp/repo")
        sender = _register_session(session_manager, project.id, "sender")
        now = 100.0
        calls: list[tuple[str, str, str]] = []
        allowed_values = iter([True, False])

        def monotonic() -> float:
            return now

        def allows_cross_project_build_coordinator(
            *,
            from_session_id: str,
            build_project_id: str,
            task_id: str,
        ) -> bool:
            calls.append((from_session_id, build_project_id, task_id))
            return next(allowed_values)

        monkeypatch.setattr(mailbox_module.time, "monotonic", monotonic)
        monkeypatch.setattr(
            mailbox,
            "_allows_cross_project_build_coordinator",
            allows_cross_project_build_coordinator,
        )

        assert (
            mailbox._allows_cached_cross_project_build_coordinator(
                from_session_id=sender.id,
                build_project_id="project",
                task_id=None,
            )
            is False
        )
        assert calls == []

        assert mailbox._allows_cached_cross_project_build_coordinator(
            from_session_id=sender.id,
            build_project_id="project",
            task_id="task",
        )
        assert mailbox._allows_cached_cross_project_build_coordinator(
            from_session_id=sender.id,
            build_project_id="project",
            task_id="task",
        )
        assert calls == [(sender.id, "project", "task")]

        now = 131.0
        assert (
            mailbox._allows_cached_cross_project_build_coordinator(
                from_session_id=sender.id,
                build_project_id="project",
                task_id="task",
            )
            is False
        )
        assert calls == [(sender.id, "project", "task"), (sender.id, "project", "task")]

    def test_agent_cross_project_auth_cache_invalidates_missing_sender(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mailbox = _mailbox(temp_db, session_manager)
        project = LocalProjectManager(temp_db).create(name="sender-project", repo_path="/tmp/repo")
        sender = _register_session(session_manager, project.id, "sender")
        calls = 0

        def allows_cross_project_build_coordinator(
            *,
            from_session_id: str,
            build_project_id: str,
            task_id: str,
        ) -> bool:
            nonlocal calls
            calls += 1
            return True

        monkeypatch.setattr(
            mailbox,
            "_allows_cross_project_build_coordinator",
            allows_cross_project_build_coordinator,
        )

        assert mailbox._allows_cached_cross_project_build_coordinator(
            from_session_id=sender.id,
            build_project_id="project",
            task_id="task",
        )

        original_get = session_manager.get

        def get_session(session_id: str) -> Session | None:
            if session_id == sender.id:
                return None
            return original_get(session_id)

        monkeypatch.setattr(session_manager, "get", get_session)

        assert (
            mailbox._allows_cached_cross_project_build_coordinator(
                from_session_id=sender.id,
                build_project_id="project",
                task_id="task",
            )
            is False
        )
        assert calls == 1
        assert mailbox._agent_cross_project_auth_cache == {}

    def test_agent_cross_project_auth_cache_is_bounded(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mailbox = _mailbox(temp_db, session_manager)
        project = LocalProjectManager(temp_db).create(name="sender-project", repo_path="/tmp/repo")
        sender = _register_session(session_manager, project.id, "sender")

        def allows_cross_project_build_coordinator(
            *,
            from_session_id: str,
            build_project_id: str,
            task_id: str,
        ) -> bool:
            return True

        monkeypatch.setattr(mailbox_module, "AGENT_CROSS_PROJECT_AUTH_CACHE_MAX_SIZE", 2)
        monkeypatch.setattr(
            mailbox,
            "_allows_cross_project_build_coordinator",
            allows_cross_project_build_coordinator,
        )

        for task_id in ("task-1", "task-2", "task-3"):
            assert mailbox._allows_cached_cross_project_build_coordinator(
                from_session_id=sender.id,
                build_project_id="project",
                task_id=task_id,
            )

        assert list(mailbox._agent_cross_project_auth_cache) == [
            (sender.id, "task-2"),
            (sender.id, "task-3"),
        ]

    @pytest.mark.asyncio
    async def test_build_target_only_includes_active_agents_in_task_subtree(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        tasks = LocalTaskManager(temp_db)
        agent_runs = LocalAgentRunManager(temp_db)
        history = BuildHistoryStorage(temp_db)
        root = tasks.create_task(sample_project["id"], "Build root")
        child_task = tasks.create_task(
            sample_project["id"],
            "Build child",
            parent_task_id=root.id,
        )
        outside_task = tasks.create_task(sample_project["id"], "Outside")
        build_run = history.record_run(
            project_id=sample_project["id"],
            action="build",
            status="started",
            root_task_id=root.id,
            input_ref=f"#{root.seq_num}",
        )

        sender = _register_session(session_manager, sample_project["id"], "sender")
        parent = _register_session(session_manager, sample_project["id"], "parent")
        subtree_child = _register_session(
            session_manager,
            sample_project["id"],
            "subtree-child",
            agent_depth=1,
        )
        outside_child = _register_session(
            session_manager,
            sample_project["id"],
            "outside-child",
            agent_depth=1,
        )
        completed_child = _register_session(
            session_manager,
            sample_project["id"],
            "completed-child",
            agent_depth=1,
        )
        agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=subtree_child.id,
            provider="codex",
            prompt="subtree",
            task_id=child_task.id,
        )
        agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=outside_child.id,
            provider="codex",
            prompt="outside",
            task_id=outside_task.id,
        )
        completed = agent_runs.create(
            parent_session_id=parent.id,
            child_session_id=completed_child.id,
            provider="codex",
            prompt="completed",
            task_id=child_task.id,
        )
        agent_runs.complete(completed.id, "done")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="build",
            target_id=build_run.id,
            content="Build update",
        )

        assert result.recipient_session_ids == [subtree_child.id]
        assert result.broadcast_id
        assert result.selector_metadata
        assert result.selector_metadata["root_task_id"] == root.id
        assert result.selector_metadata["build_run_id"] == build_run.id

    @pytest.mark.asyncio
    async def test_rejects_unknown_target(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="Unknown message target"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="workspace",
                content="Invalid",
            )

    @pytest.mark.asyncio
    async def test_rejects_unknown_target_id(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="Project target not found"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="project",
                target_id="missing-project",
                content="Invalid",
            )

    @pytest.mark.asyncio
    async def test_rejects_empty_content(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="content is required"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="all",
                content="  ",
            )
