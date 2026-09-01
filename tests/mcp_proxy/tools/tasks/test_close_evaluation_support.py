"""Claim-window derivation for the close-time commit autolink."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import claimed_session_window_start
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit

TASK_ID = "00000000-0000-4000-8000-000000000101"
OWNER = "00000000-0000-4000-8000-000000000301"
EARLIER_SESSION = "00000000-0000-4000-8000-000000000302"


def _row(session_id: str, action: str, hour: int, minute: int = 0) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "action": action,
        "created_at": datetime(2026, 9, 1, hour, minute, tzinfo=UTC),
    }


# Storage returns rows newest first.
ROWS = [
    _row(OWNER, "claimed", 13, 30),
    _row(OWNER, "claimed", 13),
    _row(EARLIER_SESSION, "worked_on", 12, 30),
    _row(EARLIER_SESSION, "claimed", 12),
    _row(EARLIER_SESSION, "created", 11),
]


def _ctx(rows: list[dict[str, Any]] | Exception) -> RegistryContext:
    def get_task_sessions(_task_id: str) -> list[dict[str, Any]]:
        if isinstance(rows, Exception):
            raise rows
        return rows

    return cast(
        RegistryContext,
        SimpleNamespace(session_task_manager=SimpleNamespace(get_task_sessions=get_task_sessions)),
    )


def _task(owner: str | None) -> Task:
    return Task(
        id=TASK_ID,
        project_id="00000000-0000-4000-8000-000000000201",
        title="Windowed leaf",
        category="code",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        claimed_by_session_id=owner,
    )


def test_owned_task_uses_the_owners_latest_claim() -> None:
    window = claimed_session_window_start(_ctx(ROWS), _task(OWNER), TASK_ID)

    assert window == "2026-09-01T13:30:00+00:00"


def test_unowned_task_falls_back_to_the_earliest_linked_window() -> None:
    # Escalation cleared the owner; the earlier claimant's window still bounds the scan,
    # and a bare "created" link does not count as evidence.
    window = claimed_session_window_start(_ctx(ROWS), _task(None), TASK_ID)

    assert window == "2026-09-01T12:00:00+00:00"


def test_unowned_task_without_evidence_links_has_no_window() -> None:
    window = claimed_session_window_start(_ctx([_row(OWNER, "created", 11)]), _task(None), TASK_ID)

    assert window is None


def test_unowned_task_with_unreadable_history_has_no_window() -> None:
    window = claimed_session_window_start(_ctx(RuntimeError("db down")), _task(None), TASK_ID)

    assert window is None
