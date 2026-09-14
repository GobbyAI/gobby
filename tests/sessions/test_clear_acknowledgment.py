"""Terminal-clear acknowledgment, awaiting_handoff transitions, pending reuse, resume parking."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session_start.handoff import rebind_resumed_session_start
from gobby.mcp_proxy.tools.sessions import _terminal_clear
from gobby.sessions.clear_continuation import (
    clear_failed_attempt,
    mark_clear_command_sent,
    pending_clear_attempt,
    stage_clear_attempt,
    take_clear_handoff_marker,
)
from gobby.sessions.handoff import consume_pending_handoff
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

_PENDING_ATTEMPT_ID = "f" * 32


def _terminal_context(pane: str = "%91") -> dict[str, Any]:
    return {
        "tmux_pane": pane,
        "tmux_socket_path": "/tmp/gobby-clear-test",
        "parent_pid": 4242,
        "parent_create_time": 1.0,
    }


class _Pane:
    """PaneIO fake showing a fixed capture and recording the keys it receives."""

    backend = "tmux"
    target = "%91"

    def __init__(self, text: str = "> ") -> None:
        self.text = text
        self.keys: list[str] = []

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        return True, None

    async def snapshot(self, lines: int = 12) -> str | None:
        return self.text


def _register_predecessor(
    db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SessionManager, Any]:
    isolated = install_isolated_checkout_project(
        db, tmp_path / "checkout", name="clear-acknowledgment", monkeypatch=monkeypatch
    )
    sessions = SessionManager(db)
    predecessor_id = sessions.register_session(
        external_id="provider-before-clear",
        machine_id=isolated.machine_id,
        source="grok",
        project_id=isolated.project.id,
        terminal_context=_terminal_context(),
    )
    predecessor = sessions.get(predecessor_id)
    assert predecessor is not None
    return sessions, predecessor


def _status(sessions: SessionManager, session_id: str) -> str | None:
    row = sessions.get(session_id)
    return None if row is None else row.status


def _patches(predecessor: Any, pane: _Pane, send_command: Any) -> list[Any]:
    return [
        patch.object(_terminal_clear, "get_current_session_id", return_value=predecessor.id),
        patch.object(
            _terminal_clear,
            "_resolve_session_for_compaction",
            return_value=(predecessor.id, predecessor, None),
        ),
        patch.object(
            _terminal_clear,
            "_authorize_send_keys_target",
            return_value=(predecessor.id, None),
        ),
        patch.object(_terminal_clear, "_resolve_pane_io", return_value=(pane, None)),
        # Grok fails closed without a transcript to observe (#22358); the sender is
        # faked here, so the observer is irrelevant to acknowledgment semantics.
        patch.object(_terminal_clear, "_interrupt_observer", return_value=(None, None)),
        patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
    ]


async def _run_clear(
    markdown: str,
    *,
    sessions: SessionManager,
    db: HubDatabase,
    patches: list[Any],
) -> dict[str, Any]:
    agent_runs = MagicMock()
    agent_runs.get_by_session.return_value = None
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        return await _terminal_clear.execute_clear_session(
            build_handoff_payload(
                current_state=markdown.removeprefix("## Current State\n\n"),
                next_steps=["Continue."],
            ),
            session_manager=sessions,
            db=db,
            agent_run_manager=agent_runs,
        )


@pytest.mark.asyncio
async def test_clear_acknowledges_bound_successor_and_handoff_is_one_shot(
    hub_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, predecessor = _register_predecessor(hub_db, tmp_path, monkeypatch)
    successor_id: str | None = None
    attempt_id: str | None = None
    status_while_sending: str | None = None
    real_stage = stage_clear_attempt

    def stage_with_capture(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempt_id
        attempt_id = kwargs["attempt_id"]
        return real_stage(*args, **kwargs)

    async def send_command(
        _pane: Any,
        _command: str,
        _session_id: str,
        **_kwargs: Any,
    ) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
        nonlocal successor_id, status_while_sending
        assert attempt_id is not None
        status_while_sending = _status(sessions, predecessor.id)
        successor_id = sessions.register_session(
            external_id="provider-after-clear",
            machine_id=predecessor.machine_id,
            source=predecessor.source,
            project_id=predecessor.project_id,
            terminal_context=_terminal_context(),
        )
        assert take_clear_handoff_marker(
            hub_db,
            predecessor.id,
            attempt_id=attempt_id,
            successor_id=successor_id,
        )
        return True, None, True, None

    patches = _patches(predecessor, _Pane(), send_command)
    patches.append(
        patch.object(_terminal_clear, "stage_clear_attempt", side_effect=stage_with_capture)
    )
    result = await _run_clear(
        "## Current State\n\nContinue on the successor.",
        sessions=sessions,
        db=hub_db,
        patches=patches,
    )

    assert successor_id is not None
    assert result["success"] is True
    assert result["command_sent"] is True
    assert result["reused_attempt"] is False
    assert result.get("successor_id") == successor_id
    assert result.get("acknowledged_by") == "successor_binding"
    # Staging moved the row to awaiting_handoff; only the successor's bind expires it.
    assert status_while_sending == "awaiting_handoff"
    assert _status(sessions, predecessor.id) == "expired"
    assert pending_clear_attempt(hub_db, predecessor.id) is None
    handoff = consume_pending_handoff(hub_db, successor_id)
    assert handoff is not None
    assert "Continue on the successor" in handoff.markdown
    assert consume_pending_handoff(hub_db, successor_id) is None


@pytest.mark.asyncio
async def test_clear_timeout_keeps_the_delivered_attempt_pending(
    hub_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, predecessor = _register_predecessor(hub_db, tmp_path, monkeypatch)
    patches = _patches(predecessor, _Pane(), AsyncMock(return_value=(True, None, True, None)))
    patches.append(patch.object(_terminal_clear, "_CLEAR_ACK_TIMEOUT_SECONDS", 0.0))

    result = await _run_clear(
        "## Current State\n\nThis attempt must stay staged.",
        sessions=sessions,
        db=hub_db,
        patches=patches,
    )

    assert result["success"] is False
    assert result["error_code"] == "clear_acknowledgment_timeout"
    assert result["command_sent"] is True
    assert result["attempt_restored"] is False
    assert result["attempt_pending"] is True
    assert result["reused_attempt"] is False
    pending = pending_clear_attempt(hub_db, predecessor.id)
    assert pending is not None
    assert pending["attempt_id"] == result["attempt_id"]
    assert pending["command_sent_at"]
    assert _status(sessions, predecessor.id) == "awaiting_handoff"


@pytest.mark.asyncio
async def test_pre_send_failure_restores_the_row_and_attempt(
    hub_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, predecessor = _register_predecessor(hub_db, tmp_path, monkeypatch)
    assert _status(sessions, predecessor.id) == "active"
    failure = (
        False,
        "CLI did not confirm interruption after 3 attempts",
        False,
        {"error_code": "interrupt_unconfirmed", "continuation_pending": False},
    )

    result = await _run_clear(
        "## Current State\n\nNever delivered.",
        sessions=sessions,
        db=hub_db,
        patches=_patches(predecessor, _Pane(), AsyncMock(return_value=failure)),
    )

    assert result["success"] is False
    assert result["error_code"] == "interrupt_unconfirmed"
    assert pending_clear_attempt(hub_db, predecessor.id) is None
    assert consume_pending_handoff(hub_db, predecessor.id) is None
    assert _status(sessions, predecessor.id) == "active"


@pytest.mark.asyncio
async def test_clear_acknowledges_fresh_provider_session_before_binding(
    hub_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, predecessor = _register_predecessor(hub_db, tmp_path, monkeypatch)
    observed_id: str | None = None

    async def send_command(
        _pane: Any,
        _command: str,
        _session_id: str,
        **_kwargs: Any,
    ) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
        nonlocal observed_id
        observed_id = sessions.register_session(
            external_id="provider-after-clear",
            machine_id=predecessor.machine_id,
            source=predecessor.source,
            project_id=predecessor.project_id,
            terminal_context=_terminal_context(),
        )
        return True, None, True, None

    result = await _run_clear(
        "## Current State\n\nProvider session observed.",
        sessions=sessions,
        db=hub_db,
        patches=_patches(predecessor, _Pane(), send_command),
    )

    assert observed_id is not None
    assert result["success"] is True
    assert result["acknowledged_by"] == "provider_session"
    assert result["observed_session_id"] == observed_id
    assert "successor_id" not in result


@pytest.mark.asyncio
async def test_pending_attempt_is_reused_and_its_content_refreshed(
    hub_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, predecessor = _register_predecessor(hub_db, tmp_path, monkeypatch)
    stage_clear_attempt(
        hub_db,
        predecessor.id,
        attempt_id=_PENDING_ATTEMPT_ID,
        handoff=build_handoff_payload(
            current_state="First attempt content.",
            next_steps=["Continue."],
        ),
        terminal_context=_terminal_context(),
        chat_context=None,
    )
    assert mark_clear_command_sent(hub_db, predecessor.id, attempt_id=_PENDING_ATTEMPT_ID)
    pane = _Pane("> ")
    send_command = AsyncMock()
    patches = _patches(predecessor, pane, send_command)
    patches.append(patch.object(_terminal_clear, "_CLEAR_ACK_TIMEOUT_SECONDS", 0.0))

    result = await _run_clear(
        "## Current State\n\nRefreshed content.",
        sessions=sessions,
        db=hub_db,
        patches=patches,
    )

    assert result["error_code"] == "clear_acknowledgment_timeout"
    assert result["reused_attempt"] is True
    assert result["attempt_pending"] is True
    assert result["attempt_id"] == _PENDING_ATTEMPT_ID
    send_command.assert_not_awaited()
    # A retry never touches the pane; the delivered command stands.
    assert pane.keys == []
    assert _status(sessions, predecessor.id) == "awaiting_handoff"

    successor_id = sessions.register_session(
        external_id="provider-after-clear",
        machine_id=predecessor.machine_id,
        source=predecessor.source,
        project_id=predecessor.project_id,
        terminal_context=_terminal_context(),
    )
    assert take_clear_handoff_marker(
        hub_db,
        predecessor.id,
        attempt_id=_PENDING_ATTEMPT_ID,
        successor_id=successor_id,
    )
    handoff = consume_pending_handoff(hub_db, successor_id)
    assert handoff is not None
    assert "Refreshed content" in handoff.markdown
    assert "First attempt content" not in handoff.markdown


def test_pending_clear_attempt_parks_explicit_resume(
    hub_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, predecessor = _register_predecessor(hub_db, tmp_path, monkeypatch)
    attempt_state = stage_clear_attempt(
        hub_db,
        predecessor.id,
        attempt_id=_PENDING_ATTEMPT_ID,
        handoff=build_handoff_payload(
            current_state="Park the resume.",
            next_steps=["Continue."],
        ),
        terminal_context=_terminal_context(),
        chat_context=None,
    )
    handler = MagicMock()
    handler._session_manager = sessions
    # Mirror SessionStartHandler._derive_transcript_path's stored-path fallback.
    handler._derive_transcript_path = MagicMock(
        side_effect=lambda *args, **kwargs: kwargs.get("stored_path")
    )

    with patch.object(
        sessions,
        "rebind_resumed_terminal_session",
        wraps=sessions.rebind_resumed_terminal_session,
    ) as rebind:
        resumed, transcript_path = rebind_resumed_session_start(
            handler,
            {"source": "resume"},
            predecessor,
            machine_id=predecessor.machine_id,
            project_id=predecessor.project_id,
            cli_source=predecessor.source,
            terminal_context=_terminal_context(),
            transcript_path="/tmp/resumed.jsonl",
        )

    assert resumed is None
    assert transcript_path == "/tmp/resumed.jsonl"
    rebind.assert_not_called()

    assert clear_failed_attempt(
        hub_db,
        predecessor.id,
        attempt_id=_PENDING_ATTEMPT_ID,
        attempt_state=attempt_state,
    )
    resumed_after_restore, _ = rebind_resumed_session_start(
        handler,
        {"source": "resume"},
        predecessor,
        machine_id=predecessor.machine_id,
        project_id=predecessor.project_id,
        cli_source=predecessor.source,
        terminal_context=_terminal_context(),
        transcript_path="/tmp/resumed.jsonl",
    )
    assert resumed_after_restore is not None
