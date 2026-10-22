"""Focused coverage for awake-time stagnation and tool activity classification."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from gobby.autonomous.progress_tracker import (
    ProgressTracker,
    ProgressType,
    _tool_activity_details,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.host_sleep import AwakeClock

pytestmark = pytest.mark.unit


def _create_session(db: HubDatabase) -> str:
    return (
        SessionManager(db)
        .register(
            external_id=f"progress-{uuid4()}",
            machine_id=None,
            source="codex",
            project_id=None,
        )
        .id
    )


def _backdate_progress(db: HubDatabase, session_id: str, *, seconds: float) -> None:
    db.execute(
        "UPDATE loop_progress SET recorded_at = %s WHERE session_id = %s",
        ((datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(), session_id),
    )


def test_host_sleep_does_not_count_as_stagnation(temp_db: HubDatabase) -> None:
    slept = [0.0]
    clock = AwakeClock(wall=time.time, monotonic=lambda: time.monotonic() - slept[0])
    session_id = _create_session(temp_db)
    tracker = ProgressTracker(temp_db, stagnation_threshold=60, clock=clock)
    tracker.record_event(session_id, ProgressType.FILE_MODIFIED)

    # The host sleeps 120 seconds after the event: wall time moves, monotonic time does not.
    _backdate_progress(temp_db, session_id, seconds=120)
    slept[0] = 120.0
    assert tracker.get_summary(session_id).is_stagnant is False

    # Another 120 awake seconds without progress is stagnation again.
    _backdate_progress(temp_db, session_id, seconds=240)
    assert tracker.get_summary(session_id).is_stagnant is True


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("Bash", {"command": "sleep 45"}),
        (
            "exec_command",
            {"cmd": "sleep 45", "workdir": "/repo", "yield_time_ms": 30000},
        ),
        ("write_stdin", {"chars": ""}),
    ],
    ids=["claude-bash-sleep", "codex-exec-command-sleep", "codex-empty-write-stdin"],
)
def test_tool_activity_details_classifies_argument_passive_waits(
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    _, is_passive_wait = _tool_activity_details(tool_name, tool_args)

    assert is_passive_wait is True


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("Bash", {"command": "sleep 45 && uv run pytest tests/x.py"}),
        ("Bash", {"command": "uv run pytest tests/x.py"}),
        ("write_stdin", {"chars": "continue\n"}),
    ],
    ids=["sleep-command-chain", "non-sleep-command", "non-empty-write-stdin"],
)
def test_tool_activity_details_keeps_active_calls_non_passive(
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    _, is_passive_wait = _tool_activity_details(tool_name, tool_args)

    assert is_passive_wait is False
