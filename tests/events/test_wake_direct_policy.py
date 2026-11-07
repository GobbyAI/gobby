"""Direct wakes follow one composer and turn policy for every provider (#25830).

An idle seat at an empty composer with no open turn takes ``[Gobby] Check messages``
at once, whatever its provider and claimed task. An active turn, a composer that is
not provably empty, and an interrupted or awaiting session get no typed input: the
durable message waits for the hook mailbox or the next confirmed idle prompt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.integration

_ACTIVE_AT = datetime(2026, 11, 6, 13, 22, tzinfo=UTC)
_EMPTY = TerminalActivity(ComposerRead("empty"))
_TURN_OPEN = TerminalActivity(ComposerRead("empty"), "transcript-turn-open")
_CLAUDE_IDLE = "────────────────────\n❯ \n────────────────────"
_CODEX_IDLE = (
    "done\n› \n\n  GPT-6-Sol xhigh · ~/Projects/gobby · 0.5.0\n  ← for agents · ? for shortcuts"
)
_CODEX_DRAFT = (
    "done\n› half-typed reply\n\n  GPT-6-Sol xhigh · ~/Projects/gobby · 0.5.0\n"
    "  ← for agents · ? for shortcuts"
)
_SHELL_PID = 4321


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


async def _flush(_session_id: str) -> None:
    return None


async def _cancel_followups(dispatcher: WakeDispatcher) -> None:
    """Cancel the bounded retries a withheld wake schedules."""
    for task in list(dispatcher._composer_retries.values()):
        task.cancel()
    for task in list(dispatcher._deferred_refreshes.values()):
        task.cancel()
    await asyncio.sleep(0)


def _seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    *,
    source: str,
    status: str,
    read: Callable[[str], TerminalActivity],
) -> tuple[str, AsyncMock, WakeDispatcher]:
    """A bound row of ``source`` at ``status`` with one direct wake message queued."""
    session_id = session_manager.register(
        external_id=f"direct-policy-{source}-{status}",
        machine_id=None,
        source=source,
        project_id=project_id,
        title=f"direct-policy-{source}-{status}",
    ).id
    with session_manager.db.transaction():
        session_manager.db.execute(
            "UPDATE sessions SET status = %s, updated_at = %s, last_activity = %s WHERE id = %s",
            (status, _ACTIVE_AT, _ACTIVE_AT, session_id),
        )
    sender_id = session_manager.register(
        external_id=f"direct-policy-sender-{source}-{status}",
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
    dispatcher.set_terminal_runtime_registry(object())
    return session_id, sender, dispatcher


@pytest.mark.asyncio
async def test_idle_empty_codex_seat_takes_direct_wake_without_a_claimed_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_target: Callable[..., None],
) -> None:
    agent_target(foreground="codex", snapshot=_CODEX_IDLE)
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="codex",
        status="idle",
        read=lambda _id: _EMPTY,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["delivered"] is True, result
    sender.assert_awaited_once()
    assert sender.await_args is not None
    assert sender.await_args.args[1] == CONTINUE_WAKE_MESSAGE


@pytest.mark.asyncio
async def test_stranded_codex_row_at_an_empty_prompt_idles_and_takes_the_wake(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_target: Callable[..., None],
) -> None:
    agent_target(foreground="codex", snapshot=_CODEX_IDLE)
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="codex",
        status="active",
        read=lambda _id: _EMPTY,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["delivered"] is True, result
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_stranded_row_without_turn_settled_evidence_keeps_the_deferred_path(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="agy",
        status="active",
        read=lambda _id: _EMPTY,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["skipped"] == "session_active"
    sender.assert_not_awaited()
    await _cancel_followups(dispatcher)


@pytest.mark.asyncio
async def test_bare_shell_target_is_never_typed_into(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_target: Callable[..., None],
) -> None:
    agent_target(foreground="zsh", snapshot=_CLAUDE_IDLE)
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="claude",
        status="idle",
        read=lambda _id: _EMPTY,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["delivered"] is False
    assert result["skipped"] == "send_keys_target_bare_shell"
    sender.assert_not_awaited()
    await _cancel_followups(dispatcher)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["Password:", "[sudo] password for josh:", "Enter API key:"])
async def test_credential_prompt_target_is_never_typed_into(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_target: Callable[..., None],
    prompt: str,
) -> None:
    agent_target(foreground="claude", snapshot=prompt)
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="claude",
        status="idle",
        read=lambda _id: _EMPTY,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["delivered"] is False
    assert result["skipped"] == "send_keys_credential_prompt"
    sender.assert_not_awaited()
    await _cancel_followups(dispatcher)


@pytest.mark.asyncio
async def test_codex_draft_visible_in_the_frame_is_never_typed_into(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_target: Callable[..., None],
) -> None:
    """The ledger reads empty, but the codex frame shows a human draft: refuse, never type."""
    agent_target(foreground="codex", snapshot=_CODEX_DRAFT)
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="codex",
        status="idle",
        read=lambda _id: _EMPTY,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["delivered"] is False
    assert result["skipped"] == "send_keys_composer_not_idle"
    sender.assert_not_awaited()
    await _cancel_followups(dispatcher)


@pytest.mark.asyncio
async def test_wake_never_types_into_an_active_turn(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="claude",
        status="active",
        read=lambda _id: _TURN_OPEN,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["skipped"] == "session_active"
    sender.assert_not_awaited()
    await _cancel_followups(dispatcher)


@pytest.mark.asyncio
async def test_held_daemon_text_is_never_typed_into(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="claude",
        status="idle",
        read=lambda _id: TerminalActivity(ComposerRead("held", "[Gobby] Check messages")),
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["skipped"] == "composer_unconfirmed"
    sender.assert_not_awaited()
    await _cancel_followups(dispatcher)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "interrupt", "gap", "provider_limit"])
async def test_unconfirmed_composer_is_never_typed_into(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    reason: str | None,
) -> None:
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="claude",
        status="idle",
        read=lambda _id: TerminalActivity(ComposerRead("unknown", reason=reason)),
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result["skipped"] == "composer_unconfirmed"
    sender.assert_not_awaited()
    await _cancel_followups(dispatcher)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["interrupted", "awaiting_input", "awaiting_approval", "awaiting_handoff"]
)
async def test_protected_session_is_never_typed_into(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    status: str,
) -> None:
    session_id, sender, dispatcher = _seat(
        temp_db,
        session_manager,
        sample_project["id"],
        source="claude",
        status=status,
        read=lambda _id: _EMPTY,
    )

    result = await dispatcher.dispatch_live_wake(session_id)

    assert result.get("delivered") is False
    sender.assert_not_awaited()
