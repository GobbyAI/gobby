"""Tests for durable mailbox delivery."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any, Protocol
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest

import gobby.sessions.mailbox as mailbox_module
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.sessions.clear_continuation import (
    resolve_clear_successor,
    stage_clear_attempt,
    take_clear_handoff_marker,
)
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.sessions.mailbox import MailboxSendResult, MailboxService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.build_history import BuildHistoryStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager, system_session_id
from gobby.storage.tasks import LocalTaskManager
from tests._timing import drain_asyncio_tasks
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory, insert_isolated_machine

pytestmark = pytest.mark.unit


class WakeDispatcherProtocol(Protocol):
    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]: ...


class FakeWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


class BatchWakeDispatcher(FakeWakeDispatcher):
    def __init__(
        self,
        message_manager: InterSessionMessageManager,
        *,
        fail: bool = False,
    ) -> None:
        super().__init__()
        self.message_manager = message_manager
        self.fail = fail
        self.batch_calls: list[tuple[list[str], str]] = []
        self.durable_seen = False

    async def dispatch_live_wakes(
        self, session_ids: list[str], *, priority: str = "normal"
    ) -> list[dict[str, Any]]:
        self.batch_calls.append((list(session_ids), priority))
        self.durable_seen = all(
            self.message_manager.get_messages(session_id) for session_id in session_ids
        )
        if self.fail:
            raise ConnectionError("native host disconnected")
        return [
            {"session_id": session_id, "delivered": True, "method": "terminal"}
            for session_id in session_ids
        ]


class FailingWakeDispatcher:
    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        raise RuntimeError(f"wake failed for {session_id}")


class StalledWakeDispatcher:
    def __init__(self, recipients: list[str], *, fast_recipient: str | None = None) -> None:
        self.recipients = recipients
        self.fast_recipient = fast_recipient
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished: set[str] = set()
        self.tasks: list[asyncio.Task[Any]] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append(session_id)
        task = asyncio.current_task()
        assert task is not None
        self.tasks.append(task)
        if len(self.calls) == len(self.recipients):
            self.started.set()
        try:
            if session_id != self.fast_recipient:
                await self.release.wait()
            return {"session_id": session_id, "delivered": True, "method": "fake"}
        finally:
            self.finished.add(session_id)


def _register_session(
    session_manager: SessionManager,
    project_id: str,
    external_id: str,
    *,
    agent_depth: int = 0,
    parent_session_id: str | None = None,
) -> Session:
    return session_manager.register(
        external_id=external_id,
        machine_id=None,
        source="codex",
        project_id=project_id,
        title=external_id,
        agent_depth=agent_depth,
        parent_session_id=parent_session_id,
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


def _consume_clear(
    db: HubDatabase,
    session_manager: SessionManager,
    predecessor: Session,
    successor: Session,
    *,
    attempt_id: str,
) -> None:
    durable_attempt_id = uuid5(NAMESPACE_URL, attempt_id).hex
    stage_clear_attempt(
        db,
        predecessor.id,
        attempt_id=durable_attempt_id,
        handoff=build_handoff_payload(current_state="Handoff ready.", next_steps=["Continue."]),
        terminal_context=None,
        chat_context=None,
    )
    assert take_clear_handoff_marker(
        db,
        predecessor.id,
        attempt_id=durable_attempt_id,
        successor_id=successor.id,
    )


def _setup_broadcast_scenario(
    isolated_checkout_factory: IsolatedCheckoutFactory,
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
    other_project_id = isolated_checkout_factory(temp_db, "other-project").project.id
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
) -> MailboxSendResult:
    return await _mailbox(temp_db, session_manager).send(
        from_session_id=sender_id,
        target="project",
        content="Broadcast",
        message_type="announcement",
        metadata={"scope": "project-agents"},
    )


class TestMailboxDirectSend:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fanout", [False, True])
    async def test_parked_tmux_wakes_finish_beyond_former_shared_deadline(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        fanout: bool,
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipients = [
            _register_session(session_manager, sample_project["id"], f"recipient-{index}").id
            for index in range(3 if fanout else 1)
        ]
        for index, recipient in enumerate(recipients):
            session_manager.update(
                recipient, status="paused", terminal_context={"tmux_pane": f"%{index}"}
            )
        started: set[str] = set()
        submitted: set[str] = set()
        all_started = asyncio.Event()
        release = asyncio.Event()
        first_submitted = asyncio.Event()

        async def send_keys(
            pane_id: str,
            message: str,
            tmux_socket_path: str | None,
            *,
            submit: bool = False,
            clear_before_submit: bool = False,
            cli_source: str | None = None,
        ) -> None:
            assert message == CONTINUE_WAKE_MESSAGE
            assert submit is True
            started.add(pane_id)
            if len(started) == len(recipients):
                all_started.set()
            # Model a slow paste/Enter sequence followed by other recipients.
            if pane_id == "%0":
                await release.wait()
            else:
                await first_submitted.wait()
            submitted.add(pane_id)
            if pane_id == "%0":
                first_submitted.set()

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=InterSessionMessageManager(temp_db),
            tmux_pane_sender=send_keys,
        )
        send_task = asyncio.create_task(
            _mailbox(temp_db, session_manager, dispatcher).send(
                from_session_id=sender.id,
                target="project" if fanout else "session",
                target_id=None if fanout else recipients[0],
                content="Durable urgent notice",
                wake=True,
            )
        )
        try:
            await asyncio.wait_for(all_started.wait(), timeout=3)
            rows = temp_db.fetchall(
                "SELECT id, to_session, content FROM inter_session_messages WHERE from_session = %s",
                (sender.id,),
            )
            assert {row["to_session"] for row in rows} == set(recipients)
            assert all(row["content"] == "Durable urgent notice" for row in rows)
            loop = asyncio.get_running_loop()
            original_time = loop.time
            with monkeypatch.context() as clock_patch:
                clock_patch.setattr(loop, "time", lambda: original_time() + 10)
                await drain_asyncio_tasks(cycles=10)
                assert not send_task.done(), "An outer timeout cancelled pending tmux submission"
                release.set()
                result = await asyncio.wait_for(send_task, timeout=3)
        finally:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)

        assert result.success is True
        assert set(result.message_ids) == {row["id"] for row in rows}
        assert len(result.message_ids) == len(recipients)
        assert [item["session_id"] for item in result.wake_results] == result.recipient_session_ids
        assert submitted == {f"%{index}" for index in range(len(recipients))}
        assert result.wake_results == [
            {
                "session_id": recipient,
                "delivered": True,
                "method": "tmux_pane",
                "session_status": "paused",
                "message_id": result.message_ids[index],
            }
            for index, recipient in enumerate(result.recipient_session_ids)
        ]
        persisted = temp_db.fetchall("SELECT id FROM inter_session_messages")
        assert {row["id"] for row in persisted} == set(result.message_ids)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("priority", ["normal", "urgent"])
    async def test_active_mailbox_wake_uses_shared_priority_policy(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        priority: str,
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")
        session_manager.update(recipient.id, terminal_context={"tmux_pane": "%7"})
        pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=InterSessionMessageManager(temp_db),
            tmux_pane_sender=pane_sender,
        )

        result = await _mailbox(temp_db, session_manager, dispatcher).send(
            from_session_id=sender.id,
            target="session",
            target_id=recipient.id,
            content="Process this message",
            wake=True,
            priority=priority,
        )

        if priority == "normal":
            assert result.wake_results == [
                {
                    "session_id": recipient.id,
                    "delivered": False,
                    "method": "next_call_context",
                    "skipped": "session_active",
                    "decline_reason": "session_active",
                    "ism_persisted": True,
                    "session_status": "active",
                    "message_id": result.message_ids[0],
                }
            ]
            pane_sender.assert_not_awaited()
        else:
            assert result.wake_results == [
                {
                    "session_id": recipient.id,
                    "delivered": True,
                    "method": "tmux_pane",
                    "session_status": "active",
                    "message_id": result.message_ids[0],
                }
            ]
            pane_sender.assert_awaited_once()
        rows = temp_db.fetchall("SELECT id, priority FROM inter_session_messages")
        assert rows == [{"id": result.message_ids[0], "priority": priority}]

    @pytest.mark.asyncio
    async def test_cancelled_urgent_send_preserves_messages_and_drains_wakes(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipients = [
            _register_session(session_manager, sample_project["id"], f"recipient-{index}").id
            for index in range(2)
        ]
        dispatcher = StalledWakeDispatcher(recipients)
        send_task = asyncio.create_task(
            _mailbox(temp_db, session_manager, dispatcher).send(
                from_session_id=sender.id,
                target="project",
                content="Persist before cancelled wake",
                wake=True,
            )
        )
        try:
            await asyncio.wait_for(dispatcher.started.wait(), timeout=3)
            send_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await send_task
        finally:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)

        assert dispatcher.finished == set(recipients)
        assert all(task.done() for task in dispatcher.tasks)
        rows = temp_db.fetchall(
            "SELECT to_session, content FROM inter_session_messages WHERE from_session = %s",
            (sender.id,),
        )
        assert len(rows) == len(recipients)
        assert {row["to_session"] for row in rows} == set(recipients)
        assert all(row["content"] == "Persist before cancelled wake" for row in rows)

    @pytest.mark.asyncio
    async def test_nonurgent_send_does_not_wait_for_dispatcher(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")
        dispatcher = StalledWakeDispatcher([recipient.id])

        result = await _mailbox(temp_db, session_manager, dispatcher).send(
            from_session_id=sender.id,
            target="session",
            target_id=recipient.id,
            content="Routine notice",
        )

        assert result.success is True
        assert result.wake_results == []
        assert dispatcher.calls == []
        row = temp_db.fetchone(
            "SELECT content FROM inter_session_messages WHERE id = %s", (result.message_ids[0],)
        )
        assert row is not None
        assert row["content"] == "Routine notice"

    def test_clear_take_retargets_agent_runs(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        predecessor = _register_session(session_manager, sample_project["id"], "predecessor")
        successor = _register_session(session_manager, sample_project["id"], "successor")
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=predecessor.id,
            provider="codex",
            prompt="retarget me",
        )

        _consume_clear(
            temp_db,
            session_manager,
            predecessor,
            successor,
            attempt_id="retarget-agent-run",
        )

        row = temp_db.fetchone("SELECT parent_session_id FROM agent_runs WHERE id = %s", (run.id,))
        assert row is not None
        assert row["parent_session_id"] == successor.id

    @pytest.mark.asyncio
    async def test_direct_send_redirects_to_clear_successor(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        predecessor = _register_session(session_manager, sample_project["id"], "predecessor")
        successor = _register_session(session_manager, sample_project["id"], "successor")
        _consume_clear(
            temp_db,
            session_manager,
            predecessor,
            successor,
            attempt_id="redirect-direct-message",
        )

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="session",
            target_id=predecessor.id,
            content="follow the clear",
            metadata={"purpose": "regression"},
        )

        assert result.recipient_session_ids == [successor.id]
        row = temp_db.fetchone(
            "SELECT to_session, metadata_json FROM inter_session_messages WHERE id = %s",
            (result.message_ids[0],),
        )
        assert row is not None
        assert row["to_session"] == successor.id
        assert json.loads(row["metadata_json"]) == {
            "purpose": "regression",
            "redirected_from": predecessor.id,
            "wake_requested": False,
        }

        get_session = session_manager.get

        def successor_disappears(session_id: str) -> Session | None:
            if session_id == successor.id:
                return None
            return get_session(session_id)

        monkeypatch.setattr(session_manager, "get", successor_disappears)
        with pytest.raises(ValueError, match=f"Recipient session not found: {successor.id}"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="session",
                target_id=predecessor.id,
                content="do not fall back",
            )

        terminal_successor = replace(successor, status="expired")

        def successor_becomes_terminal(session_id: str) -> Session | None:
            if session_id == successor.id:
                return terminal_successor
            return get_session(session_id)

        monkeypatch.setattr(session_manager, "get", successor_becomes_terminal)
        terminal_error: ValueError | None = None
        try:
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="session",
                target_id=predecessor.id,
                content="do not target a terminal successor",
            )
        except ValueError as exc:
            terminal_error = exc
        assert terminal_error is not None
        assert str(terminal_error) == f"Recipient clear successor is not live: {successor.id}"

    @pytest.mark.asyncio
    async def test_direct_send_follows_chained_clear_successors(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        first = _register_session(session_manager, sample_project["id"], "first")
        second = _register_session(session_manager, sample_project["id"], "second")
        live = _register_session(session_manager, sample_project["id"], "live")
        _consume_clear(temp_db, session_manager, first, second, attempt_id="chain-one")
        _consume_clear(temp_db, session_manager, second, live, attempt_id="chain-two")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="session",
            target_id=first.id,
            content="follow the chain",
        )

        assert result.recipient_session_ids == [live.id]
        assert resolve_clear_successor(temp_db, first.id) == live.id

    @pytest.mark.asyncio
    async def test_direct_send_keeps_live_and_non_clear_terminal_recipients(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        live = _register_session(session_manager, sample_project["id"], "live")
        terminal = _register_session(session_manager, sample_project["id"], "terminal")
        session_manager.update_status(terminal.id, "expired")
        mailbox = _mailbox(temp_db, session_manager)

        live_result = await mailbox.send(
            from_session_id=sender.id,
            target="session",
            target_id=live.id,
            content="live",
        )
        terminal_result = await mailbox.send(
            from_session_id=sender.id,
            target="session",
            target_id=terminal.id,
            content="terminal",
        )

        assert live_result.recipient_session_ids == [live.id]
        assert terminal_result.recipient_session_ids == [terminal.id]
        rows = temp_db.fetchall(
            "SELECT metadata_json FROM inter_session_messages WHERE id = ANY(%s)",
            ([live_result.message_ids[0], terminal_result.message_ids[0]],),
        )
        assert all(
            "redirected_from" not in json.loads(row["metadata_json"] or "{}") for row in rows
        )

    def test_clear_successor_resolution_is_capped_at_five_hops(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        chain = [
            _register_session(session_manager, sample_project["id"], f"hop-{index}")
            for index in range(7)
        ]
        for index, (predecessor, successor) in enumerate(zip(chain, chain[1:], strict=False)):
            _consume_clear(
                temp_db,
                session_manager,
                predecessor,
                successor,
                attempt_id=f"hop-{index}",
            )

        assert resolve_clear_successor(temp_db, chain[0].id) is None
        assert resolve_clear_successor(temp_db, chain[1].id) == chain[-1].id

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
            wake=True,
        )

        assert result.recipient_session_ids == [recipient.id]
        assert result.broadcast_id is None
        assert len(result.message_ids) == 1
        assert result.wake_results == [
            {
                "session_id": recipient.id,
                "delivered": True,
                "method": "fake",
                "session_status": "active",
                "message_id": result.message_ids[0],
            }
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
        assert json.loads(row["metadata_json"]) == {
            "task_id": "#14760",
            "wake_requested": True,
        }

    @pytest.mark.asyncio
    async def test_system_session_direct_send_uses_explicit_project_scope(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=system_session_id(),
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
            "from_session": system_session_id(),
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

        assert mailbox._resolve_project_id(system_session_id(), "") == "resolved-project"
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
    async def test_session_target_delivers_across_projects(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        other_project = isolated_checkout_factory(temp_db, "game-goblins").project
        sender = _register_session(session_manager, other_project.id, "goblins-sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="session",
            target_id=recipient.id,
            content="Cross-project hello",
        )

        assert result.success
        assert result.recipient_session_ids == [recipient.id]

    @pytest.mark.asyncio
    async def test_session_target_rejects_explicit_project_scope_mismatch(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        other_project = isolated_checkout_factory(temp_db, "game-goblins").project
        sender = _register_session(session_manager, other_project.id, "goblins-sender")
        recipient = _register_session(session_manager, sample_project["id"], "recipient")

        with pytest.raises(ValueError, match="outside the target project"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="session",
                target_id=recipient.id,
                content="Wrong scope",
                project_id=other_project.id,
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
            wake=True,
        )

        assert result.wake_results == [
            {
                "session_id": recipient.id,
                "delivered": False,
                "method": None,
                "error": "wake_dispatcher_unavailable",
                "error_code": "wake_dispatcher_unavailable",
                "error_message": "Wake dispatcher is unavailable",
                "session_status": "active",
                "message_id": result.message_ids[0],
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
            wake=True,
        )

        assert result.wake_results == [
            {
                "session_id": recipient.id,
                "delivered": False,
                "method": None,
                "error": f"wake failed for {recipient.id}",
                "error_code": "wake_dispatch_failed",
                "error_message": f"wake failed for {recipient.id}",
                "session_status": "active",
                "message_id": result.message_ids[0],
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
            content="Broadcast",
        )

        assert result.recipient_session_ids == []
        assert result.message_ids == []
        assert result.broadcast_id
        assert result.target == "project"
        assert result.target_id is None
        assert result.to_dict()["success"] is False
        assert result.to_dict()["error_code"] == "no_recipients"
        assert result.to_dict()["error"] == "No recipients matched the target selector."
        assert result.to_dict()["selector_metadata"] == {
            "scope": {
                "kind": "project",
                "machine_id": sender.machine_id,
                "project_id": sample_project["id"],
            },
            "recipient_states": [],
        }
        assert result.to_dict()["failed_broadcasts"] == []
        assert temp_db.fetchone("SELECT id FROM inter_session_messages LIMIT 1") is None

        log_record = next(
            record
            for record in caplog.records
            if record.message == "Mailbox target resolved no recipients"
        )
        assert getattr(log_record, "from_session_id", None) == sender.id
        assert getattr(log_record, "target", None) == "project"
        assert getattr(log_record, "target_id", None) is None
        assert getattr(log_record, "broadcast_id", None) == result.broadcast_id

    @pytest.mark.asyncio
    async def test_project_target_fans_out_to_active_agent_run_sessions(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            isolated_checkout_factory,
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
        )

        assert ids["child-pending"] in result.recipient_session_ids
        assert ids["child-running"] in result.recipient_session_ids
        assert ids["child-paused"] in result.recipient_session_ids
        assert result.wake_results == []
        assert result.broadcast_id
        assert len(result.message_ids) == len(result.recipient_session_ids)

    @pytest.mark.asyncio
    async def test_project_wake_batches_after_durable_fanout_and_reports_failure(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            isolated_checkout_factory,
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        message_manager = InterSessionMessageManager(temp_db)
        dispatcher = BatchWakeDispatcher(message_manager, fail=True)
        mailbox = MailboxService(
            db=temp_db,
            message_manager=message_manager,
            session_manager=session_manager,
            wake_dispatcher=dispatcher,
        )

        result = await mailbox.send(
            from_session_id=ids["sender"],
            target="project",
            content="durable batch wake",
            wake=True,
            priority="urgent",
        )

        assert dispatcher.calls == []
        assert dispatcher.batch_calls == [(result.recipient_session_ids, "urgent")]
        assert dispatcher.durable_seen is True
        assert len(result.message_ids) == len(result.recipient_session_ids)
        assert [item["session_id"] for item in result.wake_results] == result.recipient_session_ids
        assert {item["error_code"] for item in result.wake_results} == {"wake_dispatch_failed"}

    @pytest.mark.asyncio
    async def test_project_target_uses_active_parent_when_child_session_expired(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            isolated_checkout_factory,
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
        )

        assert ids["fallback-parent"] in result.recipient_session_ids
        assert ids["fallback-child"] not in result.recipient_session_ids

    @pytest.mark.asyncio
    async def test_project_target_enforces_project_scope_and_sender_exclusion(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            isolated_checkout_factory,
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
        )

        assert ids["other-project"] not in result.recipient_session_ids
        assert ids["sender"] not in result.recipient_session_ids
        assert ids["excluded-parent"] not in result.recipient_session_ids
        assert ids["excluded-child"] not in result.recipient_session_ids

    @pytest.mark.asyncio
    async def test_project_target_writes_selector_metadata(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        ids = _setup_broadcast_scenario(
            isolated_checkout_factory,
            temp_db,
            project_manager,
            session_manager,
            sample_project["id"],
        )
        result = await _send_project_broadcast(
            temp_db,
            session_manager,
            ids["sender"],
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
            assert payload["broadcast"]["target_id"] is None
            assert payload["broadcast"]["selector"] == result.selector_metadata

    @pytest.mark.asyncio
    async def test_global_target_rejects_target_id(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="target_id is not allowed"):
            await _mailbox(temp_db, session_manager).send(
                from_session_id=sender.id,
                target="global",
                target_id=sample_project["id"],
                content="Invalid",
            )

    @pytest.mark.asyncio
    async def test_global_target_reaches_live_non_system_sessions_across_projects(
        self,
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")
        active = _register_session(session_manager, sample_project["id"], "active")
        paused = _register_session(session_manager, sample_project["id"], "paused")
        interrupted = _register_session(session_manager, sample_project["id"], "interrupted")
        awaiting_input = _register_session(
            session_manager,
            sample_project["id"],
            "awaiting-input",
        )
        awaiting_approval = _register_session(
            session_manager,
            sample_project["id"],
            "awaiting-approval",
        )
        awaiting_handoff = _register_session(
            session_manager,
            sample_project["id"],
            "awaiting-handoff",
        )
        expired = _register_session(session_manager, sample_project["id"], "expired")
        foreign_machine = _register_session(
            session_manager,
            sample_project["id"],
            "foreign-machine",
        )
        session_manager.update_status(paused.id, "paused")
        session_manager.update_status(interrupted.id, "interrupted")
        session_manager.update_status(awaiting_input.id, "awaiting_input")
        session_manager.update_status(awaiting_approval.id, "awaiting_approval")
        session_manager.update_status(awaiting_handoff.id, "awaiting_handoff")
        session_manager.update_status(expired.id, "expired")
        other_project_id = isolated_checkout_factory(
            project_manager.db, "other-all-project"
        ).project.id
        foreign = _register_session(session_manager, other_project_id, "foreign-active")
        remote_system = _register_session(session_manager, sample_project["id"], "remote-system")
        foreign_machine_id = insert_isolated_machine(
            temp_db,
            "31000000-0000-4000-8000-000000000001",
        )
        with temp_db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET source = 'system' WHERE id = %s",
                (remote_system.id,),
            )
            conn.execute(
                "UPDATE sessions SET machine_id = %s WHERE id = %s",
                (foreign_machine_id, foreign_machine.id),
            )

        result = await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="global",
            content="Global notice",
        )

        assert result.recipient_session_ids == [
            active.id,
            paused.id,
            interrupted.id,
            awaiting_input.id,
            awaiting_approval.id,
            awaiting_handoff.id,
            foreign.id,
        ]
        assert remote_system.id not in result.recipient_session_ids
        assert foreign_machine.id not in result.recipient_session_ids
        assert result.broadcast_id
        assert result.selector_metadata == {
            "scope": {
                "kind": "global",
                "machine_id": sender.machine_id,
                "project_id": None,
            },
            "recipient_states": [
                {"session_id": active.id, "status": "active"},
                {"session_id": paused.id, "status": "paused"},
                {"session_id": interrupted.id, "status": "interrupted"},
                {"session_id": awaiting_input.id, "status": "awaiting_input"},
                {"session_id": awaiting_approval.id, "status": "awaiting_approval"},
                {"session_id": awaiting_handoff.id, "status": "awaiting_handoff"},
                {"session_id": foreign.id, "status": "active"},
            ],
        }

    async def test_fanout_rolls_back_every_message_when_one_insert_fails(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "tx-sender")
        first = _register_session(session_manager, sample_project["id"], "tx-first")
        second = _register_session(session_manager, sample_project["id"], "tx-second")
        manager = InterSessionMessageManager(temp_db)
        mailbox = MailboxService(
            db=temp_db,
            message_manager=manager,
            session_manager=session_manager,
        )
        original_create = manager.create_message
        call_count = 0

        def fail_on_second_insert(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("fanout insert failed")
            return original_create(**kwargs)

        monkeypatch.setattr(manager, "create_message", fail_on_second_insert)

        with pytest.raises(RuntimeError, match="fanout insert failed"):
            await mailbox.send(
                from_session_id=sender.id,
                target="project",
                content="transactional notice",
            )

        assert manager.get_messages(first.id) == []
        assert manager.get_messages(second.id) == []

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
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mailbox = _mailbox(temp_db, session_manager)
        project = isolated_checkout_factory(temp_db, "sender-project").project
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

        monkeypatch.setattr("gobby.sessions.mailbox.time.monotonic", monotonic)
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
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mailbox = _mailbox(temp_db, session_manager)
        project = isolated_checkout_factory(temp_db, "sender-project").project
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
        isolated_checkout_factory: IsolatedCheckoutFactory,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mailbox = _mailbox(temp_db, session_manager)
        project = isolated_checkout_factory(temp_db, "sender-project").project
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
        root = tasks.create_task(
            sample_project["id"],
            "Build root",
            validation_criteria="Build root completes.",
        )
        child_task = tasks.create_task(
            sample_project["id"],
            "Build child",
            parent_task_id=root.id,
            validation_criteria="Build child completes.",
        )
        outside_task = tasks.create_task(
            sample_project["id"],
            "Outside",
            validation_criteria="Outside work completes.",
        )
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
    async def test_project_target_rejects_target_id(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        sender = _register_session(session_manager, sample_project["id"], "sender")

        with pytest.raises(ValueError, match="target_id is not allowed"):
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
                target="project",
                content="  ",
            )


@pytest.mark.asyncio
async def test_parent_target_delivers_to_sender_parent(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    parent = _register_session(sessions, sample_project["id"], "parent")
    child = _register_session(
        sessions, sample_project["id"], "child", agent_depth=1, parent_session_id=parent.id
    )
    result = await _mailbox(temp_db, sessions).send(
        from_session_id=child.id, target="parent", content="status"
    )
    assert result.recipient_session_ids == [parent.id]
    assert result.selector_metadata == {"target": "parent", "session_id": parent.id}
    assert result.target_id is None


@pytest.mark.parametrize("target_id", ["other", "", " "])
def test_parent_target_rejects_target_id(
    temp_db: HubDatabase, sample_project: dict[str, Any], target_id: str
) -> None:
    sessions = SessionManager(temp_db)
    sender = _register_session(sessions, sample_project["id"], "sender")
    with pytest.raises(ValueError, match="target_id is not allowed"):
        _mailbox(temp_db, sessions).resolve_target(
            from_session_id=sender.id, target="parent", target_id=target_id
        )


def test_parent_target_rejects_sender_without_parent(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    sender = _register_session(sessions, sample_project["id"], "sender", agent_depth=1)
    with pytest.raises(ValueError, match="Sender session has no parent"):
        _mailbox(temp_db, sessions).resolve_target(
            from_session_id=sender.id, target="parent", target_id=None
        )


@pytest.mark.asyncio
async def test_parent_target_rejects_interactive_sender(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    predecessor = _register_session(sessions, sample_project["id"], "predecessor")
    successor = _register_session(
        sessions, sample_project["id"], "successor", parent_session_id=predecessor.id
    )
    # The expired predecessor's clear marker resolves back to the sender itself.
    _consume_clear(temp_db, sessions, predecessor, successor, attempt_id="interactive-parent")

    with pytest.raises(ValueError, match="only available to spawned agent sessions"):
        await _mailbox(temp_db, sessions).send(
            from_session_id=successor.id, target="parent", content="status"
        )

    row = temp_db.fetchone(
        "SELECT id FROM inter_session_messages WHERE from_session = %s", (successor.id,)
    )
    assert row is None


@pytest.mark.asyncio
async def test_send_reserves_wake_requested_for_direct_fanout_and_nonwake_rows(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    sender = _register_session(session_manager, sample_project["id"], "marker-sender")
    recipients = [
        _register_session(session_manager, sample_project["id"], f"marker-recipient-{index}")
        for index in range(2)
    ]
    mailbox = _mailbox(temp_db, session_manager, FakeWakeDispatcher())

    direct = await mailbox.send(
        from_session_id=sender.id,
        target="session",
        target_id=recipients[0].id,
        content="direct",
        wake=True,
        metadata={"wake_requested": False},
    )
    nonwake = await mailbox.send(
        from_session_id=sender.id,
        target="session",
        target_id=recipients[1].id,
        content="nonwake",
        wake=False,
        metadata={"wake_requested": True},
    )
    fanout = await mailbox.send(
        from_session_id=sender.id,
        target="project",
        content="fanout",
        wake=True,
        metadata={"wake_requested": False},
    )

    direct_row = temp_db.fetchone(
        "SELECT metadata_json FROM inter_session_messages WHERE id = %s",
        (direct.message_ids[0],),
    )
    nonwake_row = temp_db.fetchone(
        "SELECT metadata_json FROM inter_session_messages WHERE id = %s",
        (nonwake.message_ids[0],),
    )
    assert direct_row is not None
    assert json.loads(direct_row["metadata_json"])["wake_requested"] is True
    assert nonwake_row is not None
    assert json.loads(nonwake_row["metadata_json"])["wake_requested"] is False
    fanout_rows = temp_db.fetchall(
        "SELECT metadata_json FROM inter_session_messages WHERE id = ANY(%s)",
        (fanout.message_ids,),
    )
    assert fanout_rows
    assert all(json.loads(row["metadata_json"])["wake_requested"] is True for row in fanout_rows)


@pytest.mark.asyncio
async def test_mailbox_boundary_correlates_and_logs_one_decline_per_message(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender = _register_session(session_manager, sample_project["id"], "decline-sender")
    recipient = _register_session(session_manager, sample_project["id"], "decline-recipient")
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=InterSessionMessageManager(temp_db),
    )

    with caplog.at_level(logging.INFO, logger="gobby.sessions.mailbox_delivery"):
        result = await _mailbox(temp_db, session_manager, dispatcher).send(
            from_session_id=sender.id,
            target="session",
            target_id=recipient.id,
            content="declined",
            wake=True,
        )

    assert result.wake_results[0]["message_id"] == result.message_ids[0]
    assert result.wake_results[0]["decline_reason"] == "session_active"
    records = [
        record.getMessage() for record in caplog.records if "declined" in record.getMessage()
    ]
    assert records == [
        f"mailbox wake declined for session {recipient.id} message "
        f"{result.message_ids[0]}: session_active"
    ]
