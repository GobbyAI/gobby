"""Startup sweep of automatic session titles."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id

pytestmark = pytest.mark.unit


def _codex_session(session_manager: SessionManager, project_id: str, external_id: str) -> Any:
    return session_manager.register(
        external_id=external_id,
        machine_id=get_machine_id(),
        source="codex",
        project_id=project_id,
    )


def test_normalize_rewrites_heuristic_rows_to_provisional(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _codex_session(session_manager, sample_project["id"], "legacy-heuristic")
    with session_manager.db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET title = %s, title_source = 'heuristic' WHERE id = %s",
            (f"test-project#{session.seq_num}: Repair session titles", session.id),
        )
    changed: list[tuple[str, str]] = []
    session_manager.register_title_listener(lambda sid, title: changed.append((sid, title)))

    assert session_manager.normalize_automatic_title_refs() == 1

    swept = session_manager.get(session.id)
    assert swept is not None
    expected = f"test-project#{session.seq_num}: Codex"
    assert (swept.title, swept.title_source) == (expected, "provisional")
    assert changed == [(session.id, expected)]
    assert session_manager.normalize_automatic_title_refs() == 0
    with pytest.raises(ValueError, match="Invalid title_source 'heuristic'"):
        session_manager.update_title(session.id, "Anything", title_source="heuristic")


def test_normalize_keeps_task_titles_and_renames_prefix(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _codex_session(session_manager, sample_project["id"], "stale-task-prefix")
    suffix = ": Task #42 - Keep: exact suffix"
    session_manager.update_title(
        session.id, f"old-project#{session.seq_num}{suffix}", title_source="task"
    )
    changed: list[tuple[str, str]] = []
    session_manager.register_title_listener(lambda sid, title: changed.append((sid, title)))

    assert session_manager.normalize_automatic_title_refs() == 1

    swept = session_manager.get(session.id)
    assert swept is not None
    expected = f"test-project#{session.seq_num}{suffix}"
    assert (swept.title, swept.title_source) == (expected, "task")
    assert changed == [(session.id, expected)]
    sweep_source = inspect.getsource(SessionManager.normalize_automatic_title_refs)
    assert "heuristic_title" not in sweep_source
