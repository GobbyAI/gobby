"""Compact marker reads stay bounded by the marker, not unrelated session state."""

from __future__ import annotations

import tracemalloc
from typing import Any
from uuid import uuid4

import pytest

from gobby.sessions.compact_continuation_store import (
    _load_session_variables,
    pending_compact_attempt,
)
from gobby.sessions.compact_markers import HANDOFF_COMPACT_CONTINUE_VARIABLE
from gobby.sessions.handoff import PENDING_HANDOFF_VARIABLE
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.integration


@pytest.fixture
def marker_session(session_manager: SessionManager, sample_project: dict[str, Any]) -> str:
    return session_manager.register(
        external_id="bounded-compact-marker",
        machine_id=get_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    ).id


@pytest.mark.parametrize("readiness", [True, False], ids=["readiness", "pending-attempt"])
def test_marker_read_does_not_materialize_unrelated_variables(
    session_manager: SessionManager,
    marker_session: str,
    monkeypatch: pytest.MonkeyPatch,
    readiness: bool,
) -> None:
    """Real PostgreSQL projects a small marker from a multi-megabyte JSONB row."""
    db = session_manager.db
    key = HANDOFF_COMPACT_CONTINUE_VARIABLE if readiness else PENDING_HANDOFF_VARIABLE
    marker = {
        "attempt_id": "bounded-attempt",
        "handoff_record_id": str(uuid4()),
        "clear_session": False,
    }
    SessionVariableManager(db).merge_variables(
        marker_session, {key: marker, "unrelated": "x" * (4 * 1024 * 1024)}
    )
    returned_sizes: list[int] = []
    fetchone = db.fetchone

    def observe_fetchone(sql: str, params: Any = ()) -> Any:
        row = fetchone(sql, params)
        if "FROM session_variables" in sql and row is not None:
            raw = row["variables"]
            assert isinstance(raw, str)
            returned_sizes.append(len(raw.encode()))
        return row

    monkeypatch.setattr(db, "fetchone", observe_fetchone)

    def read_marker() -> Any:
        if readiness:
            return _load_session_variables(db, marker_session)
        return pending_compact_attempt(db, marker_session)

    read_marker()  # Warm connection/cursor machinery before measuring allocations.
    returned_sizes.clear()
    tracemalloc.start()
    try:
        for _ in range(10):
            result = read_marker()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(returned_sizes) == 10
    assert max(returned_sizes) < 1024, returned_sizes
    assert peak < 1024 * 1024, peak
    if readiness:
        assert result == {key: marker}
    else:
        assert result == "bounded-attempt"


@pytest.mark.parametrize("marker", [None, [], "invalid", {"clear_session": True}])
def test_pending_attempt_rejects_invalid_projected_marker(
    session_manager: SessionManager, marker_session: str, marker: Any
) -> None:
    SessionVariableManager(session_manager.db).merge_variables(
        marker_session, {PENDING_HANDOFF_VARIABLE: marker}
    )
    assert pending_compact_attempt(session_manager.db, marker_session) is None


def test_marker_read_handles_missing_session_state(
    session_manager: SessionManager, marker_session: str
) -> None:
    assert _load_session_variables(session_manager.db, marker_session) == {}
    assert pending_compact_attempt(session_manager.db, marker_session) is None
