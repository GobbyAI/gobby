"""The marker a speculative terminal expiry leaves, end to end.

Only the two SessionStart paths that guess at terminal ownership record it, and
the claim shields read it back to tell a contested expiry from a final one
(#20837).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

import pytest

from gobby.sessions.contested_expiry import (
    CONTESTED_TERMINAL_EXPIRY_VARIABLE,
    ContestedExpiryCause,
    contested_expiry_payload,
    contested_expiry_recorded_at,
    contested_expiry_stamp,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import (
    SESSION_REVIVAL_HORIZON_HOURS,
    is_contestable_terminal_expiry,
)
from gobby.storage.sessions._contested_expiry import read_session_variables

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000021"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _registered(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    session_type: str = "terminal",
) -> Session:
    manager = SessionManager(temp_db)
    session = manager.register(
        external_id=f"ext-{uuid.uuid4()}",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
        terminal_context={"tty": "/dev/ttys004"},
        session_type=session_type,
    )
    return session


@pytest.mark.parametrize("cause", ["context_reuse", "parent_registration"])
def test_a_speculative_expiry_records_the_cause_that_produced_it(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    cause: str,
) -> None:
    """Both SessionStart guesses name themselves, so the shield can trust either."""
    manager = SessionManager(temp_db)
    session = _registered(temp_db, sample_project)

    assert manager.mark_session_expired(session.id, cause=cast(ContestedExpiryCause, cause))

    variables = read_session_variables(temp_db, session.id)
    assert variables is not None
    marker = variables[CONTESTED_TERMINAL_EXPIRY_VARIABLE]
    assert marker["cause"] == cause
    recorded_at = contested_expiry_recorded_at(variables)
    assert recorded_at is not None
    assert datetime.now(UTC) - recorded_at < timedelta(minutes=1)


def test_a_speculatively_expired_terminal_session_is_contestable(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """No tmux pane anywhere: the contest here was settled on tty."""
    manager = SessionManager(temp_db)
    session = _registered(temp_db, sample_project)
    manager.mark_session_expired(session.id, cause="context_reuse")

    expired = manager.get(session.id)
    assert expired is not None
    assert is_contestable_terminal_expiry(expired, read_session_variables(temp_db, session.id))


def test_a_web_chat_session_never_earns_the_grace(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Only terminal sessions are revivable, so only they can be contested."""
    manager = SessionManager(temp_db)
    session = _registered(temp_db, sample_project, session_type="web_chat")
    manager.mark_session_expired(session.id, cause="parent_registration")

    expired = manager.get(session.id)
    assert expired is not None
    assert read_session_variables(temp_db, session.id) is None
    assert not is_contestable_terminal_expiry(expired, None)


def test_an_expiry_that_recorded_nothing_is_final(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Inactivity and tmux-kill expiries go through update_status and leave no mark."""
    manager = SessionManager(temp_db)
    session = _registered(temp_db, sample_project)
    manager.update_status(session.id, "expired")

    expired = manager.get(session.id)
    assert expired is not None
    assert not is_contestable_terminal_expiry(expired, read_session_variables(temp_db, session.id))


@pytest.mark.parametrize(
    "variables",
    [
        None,
        {},
        {CONTESTED_TERMINAL_EXPIRY_VARIABLE: "context_reuse"},
        {CONTESTED_TERMINAL_EXPIRY_VARIABLE: {"cause": "context_reuse"}},
        {CONTESTED_TERMINAL_EXPIRY_VARIABLE: {"cause": "context_reuse", "created_at": 1756000000}},
        {
            CONTESTED_TERMINAL_EXPIRY_VARIABLE: {
                "cause": "context_reuse",
                "created_at": "not a timestamp",
            }
        },
    ],
    ids=["absent", "empty", "not-an-object", "no-created-at", "numeric", "unparseable"],
)
def test_a_marker_that_does_not_say_when_grants_nothing(
    variables: dict[str, Any] | None,
) -> None:
    """The grace is bounded by when the contest happened, so an unreadable
    marker is no marker at all -- a variables row written by something else
    must not read as a contest."""
    assert contested_expiry_recorded_at(variables) is None


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-08-24T09:08:42.635625+05:00",
        "2026-08-24T09:08:42.635625Z",
        "2026-08-24T09:08:42+00:00",
        "2026-08-24T09:08:42.635+00:00",
        "2026-08-24",
        "2026-08-24T09:08:42.635625+00:00 ",
    ],
    ids=[
        "local-offset",
        "z-suffix",
        "no-fraction",
        "short-fraction",
        "date-only",
        "trailing-space",
    ],
)
def test_only_a_fixed_width_utc_stamp_is_read_as_a_contest(stamp: str) -> None:
    """The SQL guard compares this stamp as text because casting arbitrary jsonb
    to timestamptz would raise out of the sweep, and text comparison is only
    chronological on fixed-width UTC. Admitting any other shape here is what
    would let the two shields disagree about the same marker."""
    payload = {"cause": "context_reuse", "created_at": stamp}

    assert contested_expiry_recorded_at({CONTESTED_TERMINAL_EXPIRY_VARIABLE: payload}) is None


def test_the_writer_emits_a_stamp_its_own_reader_accepts() -> None:
    """The one shape both shields admit is the one the only writer produces."""
    moment = datetime(2026, 8, 24, 9, 8, 42, tzinfo=UTC)
    payload = contested_expiry_payload("context_reuse", moment)

    assert payload["created_at"] == "2026-08-24T09:08:42.000000+00:00"
    assert contested_expiry_recorded_at({CONTESTED_TERMINAL_EXPIRY_VARIABLE: payload}) == moment


@pytest.mark.parametrize(
    "cause",
    [None, "", "inactivity", "CONTEXT_REUSE", 7, {"cause": "context_reuse"}],
    ids=["absent", "empty", "unknown", "wrong-case", "numeric", "nested"],
)
def test_a_marker_whose_cause_is_not_one_of_the_two_grants_nothing(cause: Any) -> None:
    """Only the two speculative writers earn the grace, so the marker has to
    name one of them. A fresh created_at beside any other cause is a variables
    row written by something else, and reading it as a contest would park a
    genuinely dead owner's claim for the whole revival horizon."""
    payload: dict[str, Any] = {"created_at": contested_expiry_stamp(datetime.now(UTC))}
    if cause is not None:
        payload["cause"] = cause

    assert contested_expiry_recorded_at({CONTESTED_TERMINAL_EXPIRY_VARIABLE: payload}) is None


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(minutes=1), True),
        (timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS, minutes=-1), True),
        (timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS, minutes=1), False),
        (timedelta(minutes=-1), False),
    ],
    ids=["fresh", "inside-horizon", "past-horizon", "future-dated"],
)
def test_the_grace_is_bounded_by_when_the_contest_happened(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    age: timedelta,
    expected: bool,
) -> None:
    """The grace runs from the contest, and a future-dated marker is not
    evidence of anything."""
    manager = SessionManager(temp_db)
    session = _registered(temp_db, sample_project)
    manager.mark_session_expired(session.id, cause="context_reuse")
    stamped = contested_expiry_stamp(datetime.now(UTC) - age)
    temp_db.execute(
        """
        UPDATE session_variables
           SET variables = jsonb_set(variables, %s::text[], to_jsonb(%s::text))
         WHERE session_id = %s
        """,
        ([CONTESTED_TERMINAL_EXPIRY_VARIABLE, "created_at"], stamped, session.id),
    )

    expired = cast(Session, manager.get(session.id))
    variables = read_session_variables(temp_db, session.id)

    assert is_contestable_terminal_expiry(expired, variables) is expected


def test_winning_a_contest_discards_the_marker_that_recorded_it(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """The marker describes one expiry, so surviving that expiry ends it."""
    manager = SessionManager(temp_db)
    session = _registered(temp_db, sample_project)
    manager.mark_session_expired(session.id, cause="context_reuse")

    revived = manager.revive_expired_terminal_session(session.id)

    assert revived is not None
    assert revived.status == "active"
    variables = read_session_variables(temp_db, session.id)
    assert variables is not None
    assert CONTESTED_TERMINAL_EXPIRY_VARIABLE not in variables


def test_a_contest_survived_grants_nothing_to_the_next_expiry(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A session contested once and revived is an ordinary live session again.

    When it later dies for a reason that knows the session is finished, that
    expiry is final -- the earlier contest is over and its marker must not still
    be sitting there shielding it for the rest of the revival horizon (#20837).
    """
    manager = SessionManager(temp_db)
    session = _registered(temp_db, sample_project)
    manager.mark_session_expired(session.id, cause="context_reuse")
    assert manager.revive_expired_terminal_session(session.id) is not None

    manager.update_status(session.id, "expired")

    expired = cast(Session, manager.get(session.id))
    assert expired.status == "expired"
    assert not is_contestable_terminal_expiry(expired, read_session_variables(temp_db, session.id))
