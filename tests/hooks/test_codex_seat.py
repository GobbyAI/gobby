"""Database ownership of Codex seat TUIs for first-activity adoption (#23032)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks.codex_seat import codex_seat_available
from gobby.storage.sessions import SessionManager
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX

pytestmark = pytest.mark.integration

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000023032"
OTHER_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000023033"
SEAT_PID = 12856


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _register(
    manager: SessionManager,
    project_id: str,
    terminal_context: dict[str, Any],
    *,
    external_id: str = "01a0e8d5-0000-7000-8000-000000000001",
) -> str:
    return manager.register_session(
        external_id=external_id,
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project_id,
        terminal_context=terminal_context,
    )


def test_a_seat_no_session_mentions_is_available(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    _register(
        session_manager,
        sample_project["id"],
        {"cwd": "/repo", "parent_pid": 22512, "parent_create_time": time.time() - 500},
    )

    available = codex_seat_available(session_manager.db, LOCAL_MACHINE_ID)

    assert available(SEAT_PID, time.time() - 300) is True


def test_a_seat_recorded_by_any_session_is_owned(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    started = time.time() - 300
    session_id = _register(
        session_manager,
        sample_project["id"],
        {"cwd": "/repo", "parent_pid": SEAT_PID, "parent_create_time": started + 0.2},
    )
    session_manager.update_status(session_id, "expired")

    available = codex_seat_available(session_manager.db, LOCAL_MACHINE_ID)

    assert available(SEAT_PID, started) is False
    # A reused pid with a different start is another process.
    assert available(SEAT_PID, started + 120) is True


@pytest.mark.parametrize(
    ("seat_started_ago", "expected"),
    [(300.0, False), (-300.0, True)],
    ids=["unbound-thread-since-seat-start", "unbound-thread-before-seat-start"],
)
def test_an_unbound_thread_since_the_seat_started_makes_it_ambiguous(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    seat_started_ago: float,
    expected: bool,
) -> None:
    _register(session_manager, sample_project["id"], {"cwd": "/repo"})

    available = codex_seat_available(session_manager.db, LOCAL_MACHINE_ID)

    assert available(SEAT_PID, time.time() - seat_started_ago) is expected


def test_sessions_on_another_machine_never_own_its_seats(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    started = time.time() - 300
    _register(
        session_manager,
        sample_project["id"],
        {"cwd": "/repo", "parent_pid": SEAT_PID, "parent_create_time": started},
    )
    _register(
        session_manager,
        sample_project["id"],
        {"cwd": "/repo"},
        external_id="01a0e8d5-0000-7000-8000-000000000002",
    )

    available = codex_seat_available(session_manager.db, OTHER_MACHINE_ID)

    assert available(SEAT_PID, started) is True
