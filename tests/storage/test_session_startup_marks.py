"""The durable startup mark a deferred SessionStart leaves for its first registration (#25647)."""

from __future__ import annotations

from typing import TypedDict

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import (
    STARTUP_MARK_MAX_AGE_HOURS,
    StartupPin,
    delete_startup_mark,
    pin_startup,
    prune_startup_marks,
    record_startup_mark,
    startup_mark,
    startup_mark_exists,
)

pytestmark = pytest.mark.unit

MACHINE = "machine-25647"
THREAD = "01a1a561-0000-7000-8000-000000025647"


class _Mark(TypedDict):
    machine_id: str
    source: str
    external_id: str


MARK: _Mark = {"machine_id": MACHINE, "source": "codex", "external_id": THREAD}


def test_a_recorded_mark_answers_only_its_own_thread_source_and_machine(
    temp_db: HubDatabase,
) -> None:
    record_startup_mark(temp_db, **MARK)

    assert startup_mark_exists(temp_db, **MARK)
    assert not startup_mark_exists(temp_db, **_Mark({**MARK, "machine_id": "machine-other"}))
    assert not startup_mark_exists(temp_db, **_Mark({**MARK, "source": "claude"}))
    assert not startup_mark_exists(
        temp_db, **_Mark({**MARK, "external_id": "01a1a561-0000-7000-8000-000000025648"})
    )


def test_a_repeated_startup_keeps_one_mark_and_one_delete_consumes_it(
    temp_db: HubDatabase,
) -> None:
    record_startup_mark(temp_db, **MARK)
    record_startup_mark(temp_db, **MARK)

    rows = temp_db.fetchall(
        "SELECT external_id FROM session_startup_marks WHERE machine_id = %s", (MACHINE,)
    )
    assert [row["external_id"] for row in rows] == [THREAD]

    delete_startup_mark(temp_db, **MARK)

    assert not startup_mark_exists(temp_db, **MARK)
    # A second registration of the same thread finds nothing left to consume.
    delete_startup_mark(temp_db, **MARK)
    assert not startup_mark_exists(temp_db, **MARK)


def test_pruning_drops_only_marks_older_than_the_age_limit(temp_db: HubDatabase) -> None:
    aged = _Mark({**MARK, "external_id": "01a1a561-0000-7000-8000-000000025649"})
    record_startup_mark(temp_db, **MARK)
    record_startup_mark(temp_db, **aged)
    temp_db.execute(
        """
        UPDATE session_startup_marks
        SET created_at = clock_timestamp() - make_interval(hours => %s)
        WHERE external_id = %s
        """,
        (STARTUP_MARK_MAX_AGE_HOURS + 1, aged["external_id"]),
    )

    assert prune_startup_marks(temp_db) == 1

    assert startup_mark_exists(temp_db, **MARK)
    assert not startup_mark_exists(temp_db, **aged)
    assert prune_startup_marks(temp_db) == 0


def test_the_age_limit_outlasts_a_weekend_at_the_first_prompt() -> None:
    """A seat started on Friday evening and first prompted on Monday keeps its search."""
    assert STARTUP_MARK_MAX_AGE_HOURS >= 72


FIRST = StartupPin(cli_pid=4101, cli_create_time=1_700_000_001.25)
SECOND = StartupPin(cli_pid=4102, cli_create_time=1_700_000_002.5)


def test_a_mark_is_recorded_unpinned_and_its_first_pin_wins(temp_db: HubDatabase) -> None:
    record_startup_mark(temp_db, **MARK)
    unpinned = startup_mark(temp_db, **MARK)
    assert unpinned is not None and unpinned.pin is None

    assert pin_startup(temp_db, **MARK, pin=FIRST)
    assert not pin_startup(temp_db, **MARK, pin=SECOND)

    pinned = startup_mark(temp_db, **MARK)
    assert pinned is not None and pinned.pin == FIRST
    # The pinned CLI's own repeated startup keeps its pin.
    assert pin_startup(temp_db, **MARK, pin=FIRST)


def test_a_pin_needs_a_mark_its_own_startup_recorded(temp_db: HubDatabase) -> None:
    """Pinning never creates a mark, so only a real deferred startup is ever pinned."""
    assert not pin_startup(temp_db, **MARK, pin=FIRST)
    assert startup_mark(temp_db, **MARK) is None


def test_a_flood_of_other_startups_never_displaces_a_pin(temp_db: HubDatabase) -> None:
    """Durable pins are never evicted to make room, whatever else is recorded (H2)."""
    record_startup_mark(temp_db, **MARK)
    assert pin_startup(temp_db, **MARK, pin=FIRST)
    flood = [
        (MACHINE, "claude", f"flood-{index}", 9000 + index, 1_700_000_100.0 + index)
        for index in range(5000)
    ]
    temp_db.executemany(
        """
        INSERT INTO session_startup_marks
            (machine_id, source, external_id, cli_pid, cli_create_time)
        VALUES (%s, %s, %s, %s, %s)
        """,
        flood,
    )

    pinned = startup_mark(temp_db, **MARK)
    assert pinned is not None and pinned.pin == FIRST
    assert not pin_startup(temp_db, **MARK, pin=SECOND)


def test_a_dead_pin_is_replaced_only_by_compare_and_swap(temp_db: HubDatabase) -> None:
    record_startup_mark(temp_db, **MARK)
    assert pin_startup(temp_db, **MARK, pin=FIRST)
    stale = StartupPin(cli_pid=FIRST.cli_pid, cli_create_time=FIRST.cli_create_time + 60)

    # A displaced pin that is not the one stored changes nothing.
    assert not pin_startup(temp_db, **MARK, pin=SECOND, displaced=stale)
    assert pin_startup(temp_db, **MARK, pin=SECOND, displaced=FIRST)
    # The first replacement wins; a racing one that saw the same dead pin loses.
    third = StartupPin(cli_pid=4103, cli_create_time=1_700_000_003.0)
    assert not pin_startup(temp_db, **MARK, pin=third, displaced=FIRST)

    pinned = startup_mark(temp_db, **MARK)
    assert pinned is not None and pinned.pin == SECOND
