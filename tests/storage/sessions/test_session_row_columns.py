"""Full session rows list every column and decode terminal_context once."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._query import _SESSION_COLUMN_NAMES
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit


def test_session_column_list_matches_the_sessions_table(temp_db: HubDatabase) -> None:
    rows = temp_db.fetchall(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'sessions'"
    )

    assert sorted(_SESSION_COLUMN_NAMES) == sorted(row["column_name"] for row in rows)


def test_session_reads_decode_terminal_context_once_without_redumping(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    # The pool's jsonb loader decoded and re-dumped terminal_context, and
    # Session.from_row decoded it again (#23359).
    terminal_context = {"tmux_pane": "%7", "parent_pid": 4242}
    machine_id = require_machine_id()
    session = session_manager.register(
        external_id=f"row-columns-{uuid.uuid4()}",
        machine_id=machine_id,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=terminal_context,
    )

    with patch("json.dumps", wraps=json.dumps) as dumps:
        fetched = session_manager.get(session.id)
        listed = session_manager.list(machine_id=machine_id)

    assert dumps.call_count == 0
    assert fetched is not None
    assert fetched.terminal_context == terminal_context
    assert [row.terminal_context for row in listed if row.id == session.id] == [terminal_context]
