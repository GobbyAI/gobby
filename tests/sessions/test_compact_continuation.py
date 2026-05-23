"""Tests for compact_self continuation marker delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.sessions.compact_continuation import (
    COMPACT_SELF_CONTINUE_PROMPT,
    COMPACT_SELF_CONTINUE_VARIABLE,
    mark_compact_self_continuation_pending,
    schedule_compact_self_continuation_fallback,
)
from gobby.storage.database import LocalDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


class _FakeTmux:
    def __init__(self) -> None:
        self.sent_keys: list[tuple[str, str, bool]] = []

    async def send_keys(self, pane_id: str, text: str, *, literal: bool = False) -> bool:
        self.sent_keys.append((pane_id, text, literal))
        return True


def _make_db(tmp_path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "compact-continuation.db")
    run_migrations(db)
    return db


@pytest.mark.asyncio
async def test_fallback_consumes_pending_marker_and_sends_prompt(tmp_path) -> None:
    db = _make_db(tmp_path)
    try:
        session = SimpleNamespace(
            id="sess-1",
            terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
        )
        tmux = _FakeTmux()

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
            await drain_asyncio_tasks()

        assert tmux.sent_keys == [("%12", f"{COMPACT_SELF_CONTINUE_PROMPT}\n", True)]
        variables = SessionVariableManager(db).get_variables("sess-1")
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fallback_noops_when_marker_was_already_consumed(tmp_path) -> None:
    db = _make_db(tmp_path)
    try:
        session = SimpleNamespace(
            id="sess-1",
            terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
        )
        tmux = _FakeTmux()

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
            await drain_asyncio_tasks()

        assert tmux.sent_keys == []
        variables = SessionVariableManager(db).get_variables("sess-1")
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
    finally:
        db.close()
