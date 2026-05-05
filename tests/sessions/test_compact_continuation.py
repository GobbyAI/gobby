"""Tests for compact_self continuation marker delivery."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.compact_continuation import (
    COMPACT_SELF_CONTINUE_PROMPT,
    COMPACT_SELF_CONTINUE_VARIABLE,
    mark_compact_self_continuation_pending,
    schedule_compact_self_continuation_fallback,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


def _make_db(tmp_path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "compact-continuation.db")
    run_migrations(db)
    return db


@pytest.mark.asyncio
async def test_fallback_consumes_pending_marker_and_sends_prompt(tmp_path) -> None:
    db = _make_db(tmp_path)
    try:
        session = MagicMock()
        session.id = "sess-1"
        session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        tmux = MagicMock()
        tmux.send_keys = AsyncMock(return_value=True)

        assert mark_compact_self_continuation_pending(db, "sess-1")
        with patch(
            "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
            return_value=tmux,
        ):
            scheduled = schedule_compact_self_continuation_fallback(
                db,
                pending_session_id="sess-1",
                target_session=session,
                delay_seconds=0,
            )
            assert scheduled is True
            for _ in range(3):
                await asyncio.sleep(0)

        tmux.send_keys.assert_awaited_once_with(
            "%12",
            f"{COMPACT_SELF_CONTINUE_PROMPT}\n",
            literal=True,
        )
        variables = SessionVariableManager(db).get_variables("sess-1")
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fallback_noops_when_marker_was_already_consumed(tmp_path) -> None:
    db = _make_db(tmp_path)
    try:
        session = MagicMock()
        session.id = "sess-1"
        session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        tmux = MagicMock()
        tmux.send_keys = AsyncMock(return_value=True)

        with patch(
            "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
            return_value=tmux,
        ):
            scheduled = schedule_compact_self_continuation_fallback(
                db,
                pending_session_id="sess-1",
                target_session=session,
                delay_seconds=0,
            )
            assert scheduled is True
            for _ in range(3):
                await asyncio.sleep(0)

        tmux.send_keys.assert_not_awaited()
    finally:
        db.close()
