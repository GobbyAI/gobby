"""A Claude Code pane whose turn ended at an empty prompt takes a direct wake (#25155)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import WakeDispatcher
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.integration

_ACTIVE_AT = datetime(2026, 10, 26, 16, 20, tzinfo=UTC)
_EMPTY = TerminalActivity(ComposerRead("empty"))
_SPINNER = TerminalActivity(ComposerRead("empty"), "spinner:esc to interrupt")


@dataclass
class _Pane:
    session_id: str
    sender: AsyncMock
    messages: InterSessionMessageManager
    dispatcher: WakeDispatcher


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


async def _flush(_session_id: str) -> None:
    return None


async def _cancel_followups(dispatcher: WakeDispatcher) -> None:
    """Cancel the bounded composer retry a withheld wake schedules."""
    for task in list(dispatcher._composer_retries.values()):
        task.cancel()
    for task in list(dispatcher._deferred_refreshes.values()):
        task.cancel()
    await asyncio.sleep(0)


def _claude_pane(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    read: Callable[[str], TerminalActivity],
) -> _Pane:
    """A bound Claude Code row left active after its turn ended, with one direct wake queued."""
    session_id = session_manager.register(
        external_id="idle-claude-pane",
        machine_id=None,
        source="claude",
        project_id=project_id,
        title="idle-claude-pane",
    ).id
    with session_manager.db.transaction():
        session_manager.db.execute(
            "UPDATE sessions SET status = 'active', updated_at = %s, last_activity = %s "
            "WHERE id = %s",
            (_ACTIVE_AT, _ACTIVE_AT, session_id),
        )
    sender_id = session_manager.register(
        external_id="wake-sender",
        machine_id=None,
        source="claude",
        project_id=project_id,
        title="wake-sender",
    ).id
    messages = InterSessionMessageManager(temp_db)
    messages.create_message(
        from_session=sender_id,
        to_session=session_id,
        content="direct wake",
        metadata_json='{"wake_requested": true}',
    )
    terminal = replace(make_memory_terminal(backend="native"), session_id=session_id)
    sender = AsyncMock(return_value=None)

    async def probe(_session: object, _terminal: object | None) -> TerminalActivity:
        return read(session_id)

    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=messages,
        tmux_sender=sender,
        terminal_manager=MemoryTerminalStore(terminal),
        run_db=_run_db,
        lifecycle_refresh=_flush,
        activity_probe=probe,
    )
    return _Pane(session_id, sender, messages, dispatcher)


async def test_idle_empty_claude_pane_takes_direct_wake_at_once(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    """Turn ended, composer confirmed empty: a wake=true direct send reaches the pane live."""
    pane = _claude_pane(temp_db, session_manager, sample_project["id"], lambda _id: _EMPTY)

    result = await pane.dispatcher.dispatch_live_wake(pane.session_id)

    assert result.get("skipped") is None
    assert result["delivered"] is True
    pane.sender.assert_awaited_once()


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda _id: _SPINNER, id="turn-in-flight"),
        pytest.param(
            lambda _id: TerminalActivity(ComposerRead("draft", "half-typed reply")),
            id="draft",
        ),
        pytest.param(lambda _id: TerminalActivity(ComposerRead("unknown")), id="unknown-composer"),
    ],
)
async def test_unconfirmed_claude_pane_declines_and_keeps_message_durable(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    read: Callable[[str], TerminalActivity],
) -> None:
    """Without a confirmed empty prompt the wake is withheld, never typed, and stays queued."""
    pane = _claude_pane(temp_db, session_manager, sample_project["id"], read)
    try:
        result = await pane.dispatcher.dispatch_live_wake(pane.session_id)
        assert result["skipped"] == "session_active"
        await asyncio.wait_for(pane.dispatcher._deferred_refreshes[pane.session_id], timeout=2)

        pane.sender.assert_not_awaited()
        assert [
            m.content for m in pane.messages.get_undelivered_wake_messages(pane.session_id)
        ] == ["direct wake"]
        row = session_manager.get(pane.session_id)
        assert row is not None and row.status == "active"
    finally:
        await _cancel_followups(pane.dispatcher)


async def test_claude_wake_waits_for_open_turn_then_delivers_once_idle(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake declined mid-turn is delivered by the deferred retry once the turn settles."""
    settled = False
    release_retry = asyncio.Event()

    def read(_session_id: str) -> TerminalActivity:
        return _EMPTY if settled else _SPINNER

    async def retry_wait(_dispatcher: WakeDispatcher, _delay: float) -> None:
        await release_retry.wait()

    monkeypatch.setattr(WakeDispatcher, "_composer_retry_wait", retry_wait)
    pane = _claude_pane(temp_db, session_manager, sample_project["id"], read)
    try:
        result = await pane.dispatcher.dispatch_live_wake(pane.session_id)
        assert result["skipped"] == "session_active"
        await asyncio.wait_for(pane.dispatcher._deferred_refreshes[pane.session_id], timeout=2)
        pane.sender.assert_not_awaited()

        settled = True
        release_retry.set()
        await asyncio.wait_for(pane.dispatcher._composer_retries[pane.session_id], timeout=2)

        pane.sender.assert_awaited_once()
        row = session_manager.get(pane.session_id)
        assert row is not None and row.status == "paused"
    finally:
        await _cancel_followups(pane.dispatcher)
