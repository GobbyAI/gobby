"""The durable startup mark a deferred SessionStart leaves for its first registration (#25647)."""

from __future__ import annotations

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import (
    STARTUP_MARK_MAX_AGE_HOURS,
    delete_startup_mark,
    prune_startup_marks,
    record_startup_mark,
    startup_mark_exists,
)

pytestmark = pytest.mark.unit

MACHINE = "machine-25647"
THREAD = "01a1a561-0000-7000-8000-000000025647"
MARK = {"machine_id": MACHINE, "source": "codex", "external_id": THREAD}


def test_a_recorded_mark_answers_only_its_own_thread_source_and_machine(
    temp_db: HubDatabase,
) -> None:
    record_startup_mark(temp_db, **MARK)

    assert startup_mark_exists(temp_db, **MARK)
    assert not startup_mark_exists(temp_db, **{**MARK, "machine_id": "machine-other"})
    assert not startup_mark_exists(temp_db, **{**MARK, "source": "claude"})
    assert not startup_mark_exists(
        temp_db, **{**MARK, "external_id": "01a1a561-0000-7000-8000-000000025648"}
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
    aged = {**MARK, "external_id": "01a1a561-0000-7000-8000-000000025649"}
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
