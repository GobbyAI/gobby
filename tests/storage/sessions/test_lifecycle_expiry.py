"""Focused tests for session lifecycle expiry edge cases."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock, patch

import psutil
import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_lifecycle import _build_empty_session_prune_reference_guards
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._field_update import _FieldUpdateMixin
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

# PostgreSQL-backed tests below are marked integration individually.

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def project_id(isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase) -> str:
    """Create a project and return its ID."""
    return isolated_checkout_factory(temp_db, "test-project").project.id


@pytest.fixture
def session_mgr(temp_db: HubDatabase) -> SessionManager:
    """Create the canonical storage SessionManager under test."""
    return SessionManager(temp_db)


class _PostgresColumnCaptureDb:
    dialect = "postgres"

    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def fetchall(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, str]]:
        self.queries.append((query, params))
        if params == ("sessions",):
            return [{"name": "parent_session_id"}]
        return []


@pytest.mark.unit
def test_empty_session_prune_reference_guards_use_postgres_information_schema() -> None:
    db = _PostgresColumnCaptureDb()

    guards = _build_empty_session_prune_reference_guards(db)  # type: ignore[arg-type]

    assert any("information_schema.columns" in query for query, _params in db.queries)
    assert guards == (
        "NOT EXISTS (SELECT 1 FROM sessions ref WHERE ref.parent_session_id = sessions.id)",
    )


@pytest.mark.unit
def test_update_status_if_non_terminal_loses_race_to_expiration() -> None:
    manager = MagicMock()
    manager.get.return_value.status = "active"
    manager.db.execute.return_value.rowcount = 0

    updated = _FieldUpdateMixin.update_status_if_non_terminal(manager, "session-1", "paused")

    assert updated is None
    manager.get.assert_called_once_with("session-1")
    manager._notify_session_change.assert_not_called()
    query, params = manager.db.execute.call_args.args
    assert "status != ALL(%s)" in query
    assert params[0] == "paused"
    assert params[2] == "session-1"
    assert set(params[3]) == {"expired", "deleted"}


@pytest.mark.unit
def test_expire_stale_sessions_keeps_recently_active_untracked_terminal_session(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="old-untracked-terminal",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '25 hours',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 0
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "active"


@pytest.mark.unit
def test_expire_stale_sessions_expires_unmanaged_terminal_session(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="tracked-terminal",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        terminal_context={"tty": "/dev/ttys001"},
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '25 hours',
            updated_at = NOW() - INTERVAL '25 hours',
            last_activity = NOW() - INTERVAL '25 hours',
            status = 'paused'
        WHERE id = %s
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 1
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "expired"


@pytest.mark.integration
def test_register_leaves_creation_timestamps_to_database(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="db-owned-timestamps",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project_id,
    )

    assert session.created_at is not None
    assert session.updated_at is not None
    assert session.last_activity is not None
    row = session_mgr.db.fetchone(
        "SELECT NOW() - created_at < INTERVAL '1 minute' AS fresh FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None and row["fresh"] is True


@pytest.mark.integration
def test_expire_stale_sessions_keys_on_last_activity_not_updated_at(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="stale-activity-fresh-row",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '30 hours',
            last_activity = NOW() - INTERVAL '25 hours',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 1
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "expired"
    row = session_mgr.db.fetchone(
        "SELECT NOW() - last_activity > INTERVAL '24 hours' AS untouched "
        "FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None and row["untouched"] is True


@pytest.mark.integration
def test_idle_inactive_keys_on_last_activity(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    idle = session_mgr.register(
        external_id="idle-activity",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    busy = session_mgr.register(
        external_id="busy-activity",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET last_activity = NOW() - INTERVAL '45 minutes',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (idle.id,),
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET last_activity = CURRENT_TIMESTAMP,
            updated_at = NOW() - INTERVAL '45 minutes'
        WHERE id = %s
        """,
        (busy.id,),
    )

    paused = session_mgr.idle_inactive_active_sessions(timeout_minutes=30)

    assert paused == 1
    refreshed_idle = session_mgr.get(idle.id)
    refreshed_busy = session_mgr.get(busy.id)
    assert refreshed_idle is not None and refreshed_idle.status == "idle"
    assert refreshed_busy is not None and refreshed_busy.status == "active"


@pytest.mark.integration
def test_update_status_from_activity_bumps_last_activity(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="activity-bump",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    session_mgr.db.execute(
        "UPDATE sessions SET last_activity = NOW() - INTERVAL '2 hours' WHERE id = %s",
        (session.id,),
    )

    session_mgr.update_status_from_activity(session.id, "active")

    row = session_mgr.db.fetchone(
        "SELECT NOW() - last_activity < INTERVAL '1 minute' AS fresh FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None and row["fresh"] is True


@pytest.mark.integration
def test_update_stats_bumps_last_activity_only_on_counter_growth(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="stats-growth",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    session_mgr.update_stats(session.id, message_count=4, turn_count=2)
    session_mgr.db.execute(
        "UPDATE sessions SET last_activity = NOW() - INTERVAL '2 hours' WHERE id = %s",
        (session.id,),
    )

    session_mgr.update_stats(session.id, message_count=4, turn_count=2)
    row = session_mgr.db.fetchone(
        "SELECT NOW() - last_activity > INTERVAL '1 hour' AS stale FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None and row["stale"] is True

    session_mgr.update_stats(session.id, message_count=6, turn_count=3)
    row = session_mgr.db.fetchone(
        "SELECT NOW() - last_activity < INTERVAL '1 minute' AS fresh FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None and row["fresh"] is True


@pytest.mark.unit
def test_expire_stale_sessions_keeps_web_chat_session_with_recent_activity(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session = session_mgr.register(
        external_id="old-web-chat",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project_id,
        session_type="web_chat",
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '25 hours',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (session.id,),
    )

    expired = session_mgr.expire_stale_sessions(timeout_hours=24)

    assert expired == 0
    refreshed = session_mgr.get(session.id)
    assert refreshed is not None
    assert refreshed.status == "active"


FOREIGN_MACHINE_ID = "21000000-0000-4000-8000-000000000009"
TTY = "/dev/ttys000"


def _this_cli() -> dict[str, object]:
    cli = psutil.Process()
    return {"tty": TTY, "parent_pid": cli.pid, "parent_create_time": cli.create_time()}


def _reused_pid() -> dict[str, object]:
    cli = psutil.Process()
    return {"tty": TTY, "parent_pid": cli.pid, "parent_create_time": cli.create_time() - 3600}


def _no_start_time() -> dict[str, object]:
    return {"tty": TTY, "parent_pid": os.getpid()}


def _exited_cli() -> dict[str, object]:
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    started = psutil.Process(child.pid).create_time()
    child.communicate()
    return {"tty": TTY, "parent_pid": child.pid, "parent_create_time": started}


def _quiet_session(
    session_mgr: SessionManager,
    project_id: str,
    name: str,
    terminal_context: dict[str, object],
    *,
    hours: int = 25,
    machine_id: str = LOCAL_MACHINE_ID,
) -> str:
    """Register a paused row of ``machine_id`` whose last activity is ``hours`` old."""
    session = session_mgr.register(
        external_id=name,
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
        terminal_context=terminal_context,
    )
    session_mgr.db.execute(
        """
        UPDATE sessions
        SET created_at = NOW() - INTERVAL '40 hours',
            updated_at = NOW() - make_interval(hours => %s),
            last_activity = NOW() - make_interval(hours => %s),
            status = 'paused',
            machine_id = %s
        WHERE id = %s
        """,
        (hours, hours, machine_id, session.id),
    )
    return session.id


def _status(session_mgr: SessionManager, session_id: str) -> str:
    # Read the column: the manager refuses to load another machine's row.
    row = session_mgr.db.fetchone("SELECT status FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    return str(row["status"])


@pytest.mark.integration
def test_idle_expiry_keeps_a_session_whose_cli_is_still_running(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    session_id = _quiet_session(session_mgr, project_id, "running-cli", _this_cli())

    assert session_mgr.expire_stale_sessions(timeout_hours=24) == 0
    assert _status(session_mgr, session_id) == "paused"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("stored_process", "machine_id"),
    [
        (_exited_cli, LOCAL_MACHINE_ID),
        (_reused_pid, LOCAL_MACHINE_ID),
        (_no_start_time, LOCAL_MACHINE_ID),
        (_this_cli, FOREIGN_MACHINE_ID),
    ],
    ids=["cli-exited", "pid-reused", "no-start-time", "another-machine"],
)
def test_idle_expiry_takes_a_session_no_running_cli_vouches_for(
    session_mgr: SessionManager,
    project_id: str,
    stored_process: Callable[[], dict[str, object]],
    machine_id: str,
) -> None:
    session_id = _quiet_session(
        session_mgr, project_id, "unvouched", stored_process(), machine_id=machine_id
    )

    assert session_mgr.expire_stale_sessions(timeout_hours=24) == 1
    assert _status(session_mgr, session_id) == "expired"


@pytest.mark.integration
def test_idle_expiry_vouches_for_nothing_when_this_machine_is_unknown(
    session_mgr: SessionManager,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.storage.session_lifecycle.get_machine_id", lambda: None)
    session_id = _quiet_session(session_mgr, project_id, "running-cli", _this_cli())

    assert session_mgr.expire_stale_sessions(timeout_hours=24) == 1
    assert _status(session_mgr, session_id) == "expired"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("other_hours", "expired", "other_status"),
    [(26, 2, "expired"), (0, 1, "paused")],
    ids=["both-quiet", "other-row-busy"],
)
def test_idle_expiry_vouches_for_no_row_while_two_store_one_running_cli(
    session_mgr: SessionManager,
    project_id: str,
    other_hours: int,
    expired: int,
    other_status: str,
) -> None:
    """Nothing says which row the CLI holds, so each keeps the idle timer."""
    quiet = _quiet_session(session_mgr, project_id, "one-thread", _this_cli(), hours=30)
    other = _quiet_session(
        session_mgr, project_id, "another-thread", _this_cli(), hours=other_hours
    )

    assert session_mgr.expire_stale_sessions(timeout_hours=24) == expired
    assert _status(session_mgr, quiet) == "expired"
    assert _status(session_mgr, other) == other_status


@pytest.mark.integration
def test_empty_session_expiry_still_takes_an_unprompted_session_of_a_running_cli(
    session_mgr: SessionManager,
    project_id: str,
) -> None:
    """The two-hour sweep clears rows a stray start left; a running CLI does not hold it off."""
    session_id = _quiet_session(session_mgr, project_id, "unprompted", _this_cli(), hours=3)

    assert session_mgr.expire_empty_sessions(timeout_hours=2) == 1
    assert _status(session_mgr, session_id) == "expired"
