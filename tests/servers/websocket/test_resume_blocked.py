from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hub.caller_scope import bind_caller, reset_caller
from gobby.servers.websocket.handlers.session_observe_continue import (
    _release_source_session,
    check_resume_blocked,
    handle_continue_in_chat,
)
from gobby.servers.websocket.session_control import SessionControlMixin
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import OTHER_USER_ID

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

RUN_ID = "5fd5eeb7-5f1f-4d0c-9ec1-49bc97352fef"
SOURCE_SESSION_ID = "fc5b117b-9770-45d6-a52e-e2eeed66db36"


async def test_resume_db_checks_use_websocket_db_executor() -> None:
    db = MagicMock()
    mixin = cast(
        SessionControlMixin,
        SimpleNamespace(
            session_manager=SimpleNamespace(db=db),
            _chat_sessions={},
        ),
    )
    source_session = SimpleNamespace(id="session-1")

    with patch(
        "gobby.servers.websocket.handlers.session_observe_continue.run_db",
        new_callable=AsyncMock,
        side_effect=[None, None],
    ) as run_db:
        reason = await check_resume_blocked(mixin, source_session)

    assert reason is None
    assert run_db.await_count == 2
    agent_check, pipeline_check = run_db.await_args_list
    assert agent_check.args[0:2] == (mixin, db.fetchone)
    assert "FROM agent_runs" in agent_check.args[2]
    assert pipeline_check.args[0:2] == (mixin, db.fetchone)
    assert "FROM pipeline_executions" in pipeline_check.args[2]


def _release_mixin(
    db: HubDatabase,
    *,
    wake_result: dict[str, bool],
) -> tuple[SessionControlMixin, MagicMock, CompletionEventRegistry, AsyncMock]:
    run = SimpleNamespace(id=RUN_ID, machine_id=None)
    terminal_run = SimpleNamespace(id=RUN_ID, status="cancelled", error=None)
    manager = MagicMock()
    manager.get_by_session.return_value = run
    manager.cancel.return_value = terminal_run
    manager.get.return_value = terminal_run

    wake_callback = AsyncMock(return_value=wake_result)
    completion_registry = CompletionEventRegistry(wake_callback=wake_callback)
    completion_registry.register(RUN_ID, subscribers=[SOURCE_SESSION_ID])
    CompletionSubscriberManager(db).add_completion_subscribers(RUN_ID, [SOURCE_SESSION_ID])

    async def run_db(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        return function(*args, **kwargs)

    mixin = cast(
        SessionControlMixin,
        SimpleNamespace(
            session_manager=SimpleNamespace(db=db),
            completion_registry=completion_registry,
            run_db=run_db,
        ),
    )
    return mixin, manager, completion_registry, wake_callback


@pytest.mark.integration
@pytest.mark.parametrize(
    ("wake_result", "expected_subscribers"),
    [
        ({"ism_persisted": True}, []),
        ({"ism_persisted": False}, [SOURCE_SESSION_ID]),
    ],
)
async def test_release_source_session_settles_acknowledged_delivery(
    temp_db: HubDatabase,
    wake_result: dict[str, bool],
    expected_subscribers: list[str],
) -> None:
    mixin, manager, completion_registry, wake_callback = _release_mixin(
        temp_db,
        wake_result=wake_result,
    )
    with (
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue."
            "agent_storage.LocalAgentRunManager",
            return_value=manager,
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        await _release_source_session(mixin, SOURCE_SESSION_ID, SimpleNamespace(machine_id=None))

    assert CompletionSubscriberManager(temp_db).get_completion_subscribers(RUN_ID) == (
        expected_subscribers
    )
    assert not completion_registry.is_registered(RUN_ID)
    wake_callback.assert_awaited_once()
    assert wake_callback.call_args.args[2]["run_id"] == RUN_ID


@pytest.mark.integration
async def test_release_source_session_delivers_capture_commit_before_kill_error(
    temp_db: HubDatabase,
) -> None:
    mixin, manager, completion_registry, wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    with (
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue."
            "agent_storage.LocalAgentRunManager",
            return_value=manager,
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kill failed after capture commit"),
        ),
    ):
        with pytest.raises(RuntimeError, match="failed to kill running agent"):
            await _release_source_session(
                mixin, SOURCE_SESSION_ID, SimpleNamespace(machine_id=None)
            )

    manager.cancel.assert_not_called()
    assert CompletionSubscriberManager(temp_db).get_completion_subscribers(RUN_ID) == []
    assert not completion_registry.is_registered(RUN_ID)
    wake_callback.assert_awaited_once()


@pytest.mark.integration
async def test_release_source_session_fails_when_terminal_delivery_admission_is_closed(
    temp_db: HubDatabase,
) -> None:
    mixin, manager, completion_registry, _wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    kill_agent = AsyncMock()

    with (
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue."
            "agent_storage.LocalAgentRunManager",
            return_value=manager,
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.shielded_terminal_delivery",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
            kill_agent,
        ),
    ):
        with pytest.raises(RuntimeError, match="terminal delivery admission is closed"):
            await _release_source_session(
                mixin, SOURCE_SESSION_ID, SimpleNamespace(machine_id=None)
            )

    assert completion_registry.get_subscribers(RUN_ID) == [SOURCE_SESSION_ID]
    kill_agent.assert_not_awaited()


@pytest.mark.integration
async def test_release_source_session_cancellation_waits_for_delivery(
    temp_db: HubDatabase,
) -> None:
    mixin, manager, completion_registry, wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    capture_committed = asyncio.Event()
    release_kill = asyncio.Event()

    async def gated_kill(*_args: object, **_kwargs: object) -> dict[str, bool]:
        capture_committed.set()
        await release_kill.wait()
        return {"success": True}

    with (
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue."
            "agent_storage.LocalAgentRunManager",
            return_value=manager,
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
            side_effect=gated_kill,
        ),
    ):
        task = asyncio.create_task(
            _release_source_session(mixin, SOURCE_SESSION_ID, SimpleNamespace(machine_id=None))
        )
        await capture_committed.wait()
        task.cancel()
        release_kill.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert CompletionSubscriberManager(temp_db).get_completion_subscribers(RUN_ID) == []
    assert not completion_registry.is_registered(RUN_ID)
    wake_callback.assert_awaited_once()


async def test_release_source_session_closes_terminal_through_mixin_services(
    temp_db: HubDatabase,
) -> None:
    mixin, manager, _registry, _wake = _release_mixin(temp_db, wake_result={"ism_persisted": True})
    terminal_services = object()
    cast(Any, mixin).terminal_services = terminal_services
    kill_agent = AsyncMock(return_value={"success": True})

    with (
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue."
            "agent_storage.LocalAgentRunManager",
            return_value=manager,
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
            kill_agent,
        ),
        patch(
            "gobby.servers.websocket.handlers.session_observe_continue.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        await _release_source_session(mixin, SOURCE_SESSION_ID, SimpleNamespace(machine_id=None))

    kill_agent.assert_awaited_once()
    assert kill_agent.await_args is not None
    assert kill_agent.await_args.args[0].id == RUN_ID
    assert kill_agent.await_args.kwargs == {
        "close_terminal": True,
        "terminal_services": terminal_services,
    }


def _owned_machine(temp_db: HubDatabase, owner_user_id: str) -> str:
    temp_db.execute(
        """
        INSERT INTO users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (owner_user_id, f"{owner_user_id}@example.com", "Owner", "not-a-password"),
    )
    machine_id = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine_id, owner_user_id)
    return machine_id


async def _refused_remote_release(
    temp_db: HubDatabase,
    caller: str,
    reply: dict[str, bool],
    source_owner: str,
) -> tuple[AsyncMock, MagicMock, str]:
    machine_id = _owned_machine(temp_db, TEST_USER_ID)
    source_machine_id = _owned_machine(temp_db, source_owner)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    run = AgentRun(
        id=RUN_ID,
        parent_session_id=SOURCE_SESSION_ID,
        child_session_id=None,
        provider="claude",
        prompt="do it",
        status="running",
        created_at=stamp,
        updated_at=stamp,
        machine_id=machine_id,
        terminal_id=str(uuid4()),
        pid=None,
    )
    mixin, manager, _completion_registry, _wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    manager.get_by_session.return_value = run
    node = AsyncMock()
    node.send.return_value = reply
    source_session = SimpleNamespace(
        id=SOURCE_SESSION_ID,
        machine_id=source_machine_id,
        terminal_context=None,
    )
    token = bind_caller(caller, temp_db)
    try:
        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue."
                "agent_storage.LocalAgentRunManager",
                return_value=manager,
            ),
            patch("gobby.hub.remote_cancel.node_command_client", return_value=node),
        ):
            with pytest.raises(RuntimeError, match="failed to kill running agent") as raised:
                await _release_source_session(mixin, SOURCE_SESSION_ID, source_session)
    finally:
        reset_caller(token)
    return node, manager, str(raised.value)


@pytest.mark.integration
async def test_release_source_session_refuses_unowned_remote_kill(temp_db: HubDatabase) -> None:
    # The caller owns the source machine, so only the run's own machine guard refuses.
    node, manager, message = await _refused_remote_release(
        temp_db,
        OTHER_USER_ID,
        {"stopped": True},
        source_owner=OTHER_USER_ID,
    )

    assert message == (
        f"failed to kill running agent: agent run {RUN_ID} was not stopped: machine_not_owned"
    )
    node.send.assert_not_awaited()
    manager.cancel.assert_not_called()


@pytest.mark.integration
async def test_release_source_session_refuses_unconfirmed_remote_kill(
    temp_db: HubDatabase,
) -> None:
    node, manager, message = await _refused_remote_release(
        temp_db,
        TEST_USER_ID,
        {"stopped": False},
        source_owner=TEST_USER_ID,
    )

    assert message == (
        f"failed to kill running agent: agent run {RUN_ID} was not stopped: not_stopped"
    )
    node.send.assert_awaited_once()
    manager.cancel.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize("has_run", [True, False])
async def test_release_source_session_refuses_unowned_source_before_any_change(
    temp_db: HubDatabase,
    has_run: bool,
) -> None:
    source_machine_id = _owned_machine(temp_db, TEST_USER_ID)
    source_session = SimpleNamespace(
        id=SOURCE_SESSION_ID,
        machine_id=source_machine_id,
        terminal_context={"parent_pid": 4242},
    )
    mixin, manager, completion_registry, wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    manager.get_by_session.return_value = (
        SimpleNamespace(id=RUN_ID, machine_id=source_machine_id) if has_run else None
    )
    kill_agent = AsyncMock(return_value={"success": True})
    kill_terminal = AsyncMock(return_value=True)

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue."
                "agent_storage.LocalAgentRunManager",
                return_value=manager,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
                kill_agent,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.kill_terminal_session",
                kill_terminal,
            ),
        ):
            with pytest.raises(RuntimeError, match="machine_not_owned"):
                await _release_source_session(mixin, SOURCE_SESSION_ID, source_session)
    finally:
        reset_caller(token)

    kill_agent.assert_not_awaited()
    kill_terminal.assert_not_awaited()
    manager.cancel.assert_not_called()
    wake_callback.assert_not_awaited()
    assert completion_registry.is_registered(RUN_ID)
    assert CompletionSubscriberManager(temp_db).get_completion_subscribers(RUN_ID) == [
        SOURCE_SESSION_ID
    ]


@pytest.mark.integration
async def test_release_source_session_refuses_nonlocal_plain_terminal(
    temp_db: HubDatabase,
) -> None:
    remote_machine_id = _owned_machine(temp_db, TEST_USER_ID)
    source_session = SimpleNamespace(
        id=SOURCE_SESSION_ID,
        machine_id=remote_machine_id,
        terminal_context={"parent_pid": 4242},
    )
    mixin, manager, completion_registry, wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    manager.get_by_session.return_value = None
    kill_agent = AsyncMock(return_value={"success": True})
    kill_terminal = AsyncMock(return_value=True)

    token = bind_caller(TEST_USER_ID, temp_db)
    try:
        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue."
                "agent_storage.LocalAgentRunManager",
                return_value=manager,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
                kill_agent,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.kill_terminal_session",
                kill_terminal,
            ),
        ):
            with pytest.raises(RuntimeError, match="another machine"):
                await _release_source_session(mixin, SOURCE_SESSION_ID, source_session)
    finally:
        reset_caller(token)

    kill_terminal.assert_not_awaited()
    kill_agent.assert_not_awaited()
    manager.cancel.assert_not_called()
    wake_callback.assert_not_awaited()
    assert completion_registry.is_registered(RUN_ID)


@pytest.mark.integration
async def test_continue_in_chat_refuses_unowned_source_before_mutation(
    temp_db: HubDatabase,
) -> None:
    source_machine_id = _owned_machine(temp_db, TEST_USER_ID)
    source_session = SimpleNamespace(
        id=SOURCE_SESSION_ID,
        session_type="terminal",
        source="claude",
        external_id="sdk-session-1",
        project_id=None,
        transcript_path=None,
        machine_id=source_machine_id,
        terminal_context={"parent_pid": 4242},
    )
    session_manager = MagicMock()
    session_manager.db = temp_db
    session_manager.get.return_value = source_session

    async def run_db(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        return function(*args, **kwargs)

    send_error = AsyncMock()
    mixin = cast(
        SessionControlMixin,
        SimpleNamespace(
            session_manager=session_manager,
            _chat_sessions={},
            _send_error=send_error,
            run_db=run_db,
        ),
    )
    kill_agent = AsyncMock(return_value={"success": True})
    kill_terminal = AsyncMock(return_value=True)

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue.agent_kill.kill_agent",
                kill_agent,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.kill_terminal_session",
                kill_terminal,
            ),
        ):
            await handle_continue_in_chat(
                mixin,
                MagicMock(),
                {"source_session_id": SOURCE_SESSION_ID, "conversation_id": "conversation-1"},
            )
    finally:
        reset_caller(token)

    send_error.assert_awaited_once()
    assert send_error.await_args is not None
    assert "Failed to release source session" in send_error.await_args.args[1]
    assert [call[0] for call in session_manager.method_calls] == ["get"]
    assert mixin._chat_sessions == {}
    kill_agent.assert_not_awaited()
    kill_terminal.assert_not_awaited()
