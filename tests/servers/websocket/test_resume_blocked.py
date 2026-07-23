from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.servers.websocket.handlers.session_observe_continue import (
    _release_source_session,
    check_resume_blocked,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_resume_db_checks_use_websocket_db_executor() -> None:
    db = MagicMock()
    mixin = SimpleNamespace(
        session_manager=SimpleNamespace(db=db),
        _chat_sessions={},
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
) -> tuple[SimpleNamespace, MagicMock, CompletionEventRegistry, AsyncMock]:
    run_id = "5fd5eeb7-5f1f-4d0c-9ec1-49bc97352fef"
    source_session_id = "fc5b117b-9770-45d6-a52e-e2eeed66db36"
    run = SimpleNamespace(id=run_id)
    terminal_run = SimpleNamespace(id=run_id, status="cancelled", error=None)
    manager = MagicMock()
    manager.get_by_session.return_value = run
    manager.cancel.return_value = terminal_run
    manager.get.return_value = terminal_run

    wake_callback = AsyncMock(return_value=wake_result)
    completion_registry = CompletionEventRegistry(wake_callback=wake_callback)
    completion_registry.register(run_id, subscribers=[source_session_id])
    CompletionSubscriberManager(db).add_completion_subscribers(run_id, [source_session_id])

    async def run_db(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        return function(*args, **kwargs)

    mixin = SimpleNamespace(
        session_manager=SimpleNamespace(db=db),
        completion_registry=completion_registry,
        run_db=run_db,
    )
    return mixin, manager, completion_registry, wake_callback


@pytest.mark.integration
@pytest.mark.parametrize(
    ("wake_result", "expected_subscribers"),
    [
        ({"ism_persisted": True}, []),
        ({"ism_persisted": False}, ["fc5b117b-9770-45d6-a52e-e2eeed66db36"]),
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
    run_id = "5fd5eeb7-5f1f-4d0c-9ec1-49bc97352fef"

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
        await _release_source_session(mixin, "source-session", SimpleNamespace())

    assert CompletionSubscriberManager(temp_db).get_completion_subscribers(run_id) == (
        expected_subscribers
    )
    assert not completion_registry.is_registered(run_id)
    wake_callback.assert_awaited_once()
    assert wake_callback.call_args.args[2]["run_id"] == run_id


@pytest.mark.integration
async def test_release_source_session_delivers_capture_commit_before_kill_error(
    temp_db: HubDatabase,
) -> None:
    mixin, manager, completion_registry, wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    run_id = "5fd5eeb7-5f1f-4d0c-9ec1-49bc97352fef"

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
            await _release_source_session(mixin, "source-session", SimpleNamespace())

    manager.cancel.assert_not_called()
    assert CompletionSubscriberManager(temp_db).get_completion_subscribers(run_id) == []
    assert not completion_registry.is_registered(run_id)
    wake_callback.assert_awaited_once()


@pytest.mark.integration
async def test_release_source_session_cancellation_waits_for_delivery(
    temp_db: HubDatabase,
) -> None:
    mixin, manager, completion_registry, wake_callback = _release_mixin(
        temp_db,
        wake_result={"ism_persisted": True},
    )
    run_id = "5fd5eeb7-5f1f-4d0c-9ec1-49bc97352fef"
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
            _release_source_session(mixin, "source-session", SimpleNamespace())
        )
        await capture_committed.wait()
        task.cancel()
        release_kill.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert CompletionSubscriberManager(temp_db).get_completion_subscribers(run_id) == []
    assert not completion_registry.is_registered(run_id)
    wake_callback.assert_awaited_once()
