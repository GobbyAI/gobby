"""Positive terminal-clear acknowledgment and resume parking."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session_start.handoff import rebind_resumed_session_start
from gobby.mcp_proxy.tools.sessions import _terminal_clear
from gobby.sessions.clear_continuation import (
    clear_failed_attempt,
    stage_clear_attempt,
    take_clear_handoff_marker,
)
from gobby.sessions.handoff import consume_pending_handoff
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id


def _terminal_context(pane: str = "%91") -> dict[str, Any]:
    return {
        "tmux_pane": pane,
        "tmux_socket_path": "/tmp/gobby-clear-test",
        "parent_pid": 4242,
        "parent_create_time": 1.0,
    }


def _register_predecessor(db: HubDatabase) -> tuple[SessionManager, Any]:
    sessions = SessionManager(db)
    project = LocalProjectManager(db).create(
        name="clear-acknowledgment",
        repo_path="/tmp/clear-acknowledgment",
    )
    predecessor_id = sessions.register_session(
        external_id="provider-before-clear",
        machine_id=require_machine_id(),
        source="grok",
        project_id=project.id,
        terminal_context=_terminal_context(),
    )
    predecessor = sessions.get(predecessor_id)
    assert predecessor is not None
    return sessions, predecessor


@pytest.mark.asyncio
async def test_clear_acknowledges_bound_successor_and_handoff_is_one_shot(
    hub_db: HubDatabase,
) -> None:
    sessions, predecessor = _register_predecessor(hub_db)
    successor_id: str | None = None
    attempt_id: str | None = None
    real_stage = stage_clear_attempt

    def stage_with_capture(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempt_id
        attempt_id = kwargs["attempt_id"]
        return real_stage(*args, **kwargs)

    async def send_command(
        _tmux: Any,
        _target: str,
        _command: str,
        _session_id: str,
        **_kwargs: Any,
    ) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
        nonlocal successor_id
        assert attempt_id is not None
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

    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(return_value="ready")
    agent_runs = MagicMock()
    agent_runs.get_by_session.return_value = None
    with (
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
        patch.object(
            _terminal_clear,
            "_resolve_tmux_target",
            return_value=("%91", tmux, None),
        ),
        patch.object(_terminal_clear, "stage_clear_attempt", side_effect=stage_with_capture),
        patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
    ):
        result = await _terminal_clear.execute_clear_session(
            "## Current State\n\nContinue on the successor.",
            [],
            session_manager=sessions,
            db=hub_db,
            agent_run_manager=agent_runs,
        )

    assert successor_id is not None
    assert result["success"] is True
    assert result["command_sent"] is True
    assert result.get("successor_id") == successor_id
    assert result.get("acknowledged_by") == "successor_binding"
    handoff = consume_pending_handoff(hub_db, successor_id)
    assert handoff is not None
    assert "Continue on the successor" in handoff.markdown
    assert consume_pending_handoff(hub_db, successor_id) is None


@pytest.mark.asyncio
async def test_clear_timeout_restores_staged_attempt(hub_db: HubDatabase) -> None:
    sessions, predecessor = _register_predecessor(hub_db)
    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(return_value="ready")
    agent_runs = MagicMock()
    agent_runs.get_by_session.return_value = None
    with (
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
        patch.object(
            _terminal_clear,
            "_resolve_tmux_target",
            return_value=("%91", tmux, None),
        ),
        patch.object(
            _terminal_clear,
            "_send_terminal_compaction_command",
            new=AsyncMock(return_value=(True, None, True, None)),
        ),
        patch.object(_terminal_clear, "_CLEAR_ACK_TIMEOUT_SECONDS", 0.0, create=True),
    ):
        result = await _terminal_clear.execute_clear_session(
            "## Current State\n\nThis attempt must be restored.",
            [],
            session_manager=sessions,
            db=hub_db,
            agent_run_manager=agent_runs,
        )

    assert result["success"] is False
    assert result["error_code"] == "clear_acknowledgment_timeout"
    assert result["command_sent"] is True
    assert consume_pending_handoff(hub_db, predecessor.id) is None


@pytest.mark.asyncio
async def test_clear_acknowledges_fresh_provider_session_before_binding(
    hub_db: HubDatabase,
) -> None:
    sessions, predecessor = _register_predecessor(hub_db)
    observed_id: str | None = None

    async def send_command(
        _tmux: Any,
        _target: str,
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

    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(return_value="ready")
    agent_runs = MagicMock()
    agent_runs.get_by_session.return_value = None
    with (
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
        patch.object(
            _terminal_clear,
            "_resolve_tmux_target",
            return_value=("%91", tmux, None),
        ),
        patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
    ):
        result = await _terminal_clear.execute_clear_session(
            "## Current State\n\nProvider session observed.",
            [],
            session_manager=sessions,
            db=hub_db,
            agent_run_manager=agent_runs,
        )

    assert observed_id is not None
    assert result["success"] is True
    assert result["acknowledged_by"] == "provider_session"
    assert result["observed_session_id"] == observed_id
    assert "successor_id" not in result


def test_pending_clear_attempt_parks_explicit_resume(hub_db: HubDatabase) -> None:
    sessions, predecessor = _register_predecessor(hub_db)
    attempt_state = stage_clear_attempt(
        hub_db,
        predecessor.id,
        attempt_id="pending-clear",
        handoff_markdown="## Current State\n\nPark the resume.",
        observations=[],
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
        attempt_id="pending-clear",
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
