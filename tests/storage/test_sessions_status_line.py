"""A session's one-line status: set, trimmed, cleared, capped, and kept off updated_at."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000060887"  # _personal

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000005"

STATUS_LINE_CAP = 120


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _register(sm: SessionManager, external_id: str) -> str:
    session = sm.register(
        external_id=external_id,
        machine_id=LOCAL_MACHINE_ID,
        source="test",
        project_id=PROJECT_ID,
    )
    return session.id


def _stored(sm: SessionManager, session_id: str) -> str | None:
    row = sm.db.fetchone("SELECT status_line FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    status_line: str | None = row["status_line"]
    return status_line


def test_status_line_is_stored_as_one_printable_line(session_manager: SessionManager) -> None:
    # The sidebar draws the line into terminal cells: an ESC, a CR or a newline
    # from a seat must not reach the operator's terminal as-is.
    session_id = _register(session_manager, "status-line-control")

    assert session_manager.set_status_line(session_id, "Reviewing\x1b[2J\nthe\tticker") is True

    assert _stored(session_manager, session_id) == "Reviewing[2J the ticker"


def test_status_line_persists_and_clears(session_manager: SessionManager) -> None:
    session_id = _register(session_manager, "status-line-persist")

    assert session_manager.set_status_line(session_id, "Reviewing the ticker") is True
    assert _stored(session_manager, session_id) == "Reviewing the ticker"

    assert session_manager.set_status_line(session_id, None) is True
    assert _stored(session_manager, session_id) is None


def test_status_line_is_trimmed_and_blank_clears(session_manager: SessionManager) -> None:
    session_id = _register(session_manager, "status-line-trim")

    session_manager.set_status_line(session_id, "  Waiting on review  ")
    assert _stored(session_manager, session_id) == "Waiting on review"

    session_manager.set_status_line(session_id, "   ")
    assert _stored(session_manager, session_id) is None


def test_status_line_does_not_touch_updated_at(session_manager: SessionManager) -> None:
    session_id = _register(session_manager, "status-line-updated-at")
    before = session_manager.get(session_id)
    assert before is not None

    session_manager.set_status_line(session_id, "Working the migration")

    after = session_manager.get(session_id)
    assert after is not None
    assert after.updated_at == before.updated_at


def test_status_line_at_the_cap_is_kept_and_past_it_refused(
    session_manager: SessionManager,
) -> None:
    session_id = _register(session_manager, "status-line-cap")

    session_manager.set_status_line(session_id, "x" * STATUS_LINE_CAP)
    assert _stored(session_manager, session_id) == "x" * STATUS_LINE_CAP

    with pytest.raises(ValueError, match="120"):
        session_manager.set_status_line(session_id, "x" * (STATUS_LINE_CAP + 1))
    assert _stored(session_manager, session_id) == "x" * STATUS_LINE_CAP


def test_status_line_for_an_unknown_session_reports_false(
    session_manager: SessionManager,
) -> None:
    assert (
        session_manager.set_status_line("00000000-0000-4000-8000-000000000999", "Working") is False
    )
