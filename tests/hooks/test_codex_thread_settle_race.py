"""A shared worker's child and a --no-daemon seat racing for one Codex thread (#25905).

The claim is decided before the hook's handler runs, and settle runs after it. These
tests drive the two halves separately, interleaving a seat's start between a worker
child's claim and its settle, so the seat's credential never reaches the child.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import psutil
import pytest

from gobby.hooks.session_claimant import Claimant, SessionStartClaim, claim_session_start
from gobby.hooks.session_credential import mint_session_credential, settle_claim
from gobby.storage.codex_thread_hosts import (
    ThreadHost,
    refresh_thread_host,
    register_thread_host,
    thread_host,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import (
    StartupPin,
    delete_startup_mark,
    pin_startup,
    record_startup_mark,
)
from gobby.storage.sessions import SessionManager
from tests.hooks.test_session_credential import (
    CODEX_HOOK_PID,
    CODEX_OLD_TUI_PID,
    CODEX_PROVIDER_SESSION,
    CODEX_SHARED_HOST_PID,
    FAKE_PROCESS_START,
    SIGNING_KEY,
    TEST_MACHINE_ID,
    _local_machine_identity,
)

pytestmark = pytest.mark.unit

# Fixtures imported for this module's tests.
__all__ = ["_local_machine_identity"]

THREAD = CODEX_PROVIDER_SESSION
MISMATCH = "session_claimant_mismatch"
WORKER = ThreadHost(CODEX_SHARED_HOST_PID, FAKE_PROCESS_START)
CHILD = Claimant(
    pid=CODEX_HOOK_PID,
    parent_pid=CODEX_SHARED_HOST_PID,
    cli_pid=None,
    shared_host=True,
    create_time=FAKE_PROCESS_START,
)
SEAT_PID = os.getpid()
SEAT_STARTED = float(psutil.Process(SEAT_PID).create_time())
SEAT = Claimant(pid=CODEX_HOOK_PID, parent_pid=SEAT_PID, cli_pid=SEAT_PID, create_time=SEAT_STARTED)


def _claim(db: HubDatabase, claimant: Claimant) -> SessionStartClaim:
    return claim_session_start(
        db,
        claimant=claimant,
        daemon_pid=-1,
        external_id=THREAD,
        source="codex",
        machine_id=TEST_MACHINE_ID,
        start=True,
    )


def _settle(db: HubDatabase, claim: SessionStartClaim) -> dict[str, Any] | None:
    return settle_claim(
        db,
        claim,
        signing_key=SIGNING_KEY,
        external_id=THREAD,
        source="codex",
        machine_id=TEST_MACHINE_ID,
        start=True,
    )


def _mint(
    db: HubDatabase,
    *,
    host: ThreadHost | None = None,
    owner: StartupPin | None = None,
) -> dict[str, Any] | None:
    return mint_session_credential(
        db,
        signing_key=SIGNING_KEY,
        external_id=THREAD,
        source="codex",
        machine_id=TEST_MACHINE_ID,
        owner=owner,
        host=host,
    )


def _mark(db: HubDatabase) -> None:
    record_startup_mark(db, machine_id=TEST_MACHINE_ID, source="codex", external_id=THREAD)


def _seat_starts(db: HubDatabase) -> dict[str, Any] | None:
    """The seat's start: its claim, its handler leaving the deferred mark, its settle."""
    claim = _claim(db, SEAT)
    assert claim.proven
    _mark(db)
    return _settle(db, claim)


def _seat_row(db: HubDatabase, sessions: SessionManager, project: dict[str, Any]) -> str:
    """The seat's first hook creates its row, storing the seat as owner, and consumes the mark."""
    session = sessions.register(
        external_id=THREAD, machine_id=TEST_MACHINE_ID, source="codex", project_id=project["id"]
    )
    context = {"parent_pid": SEAT_PID, "parent_create_time": SEAT_STARTED}
    db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps(context), session.id),
    )
    delete_startup_mark(db, machine_id=TEST_MACHINE_ID, source="codex", external_id=THREAD)
    return str(session.id)


def _registered(db: HubDatabase) -> ThreadHost | None:
    return thread_host(db, machine_id=TEST_MACHINE_ID, external_id=THREAD)


@contextmanager
def _before_registration(action: Callable[[], object]) -> Iterator[None]:
    """Run ``action`` after the child's claim checks and before it writes its registration."""

    def interleaved_register(db: HubDatabase, **kwargs: Any) -> bool:
        action()
        return register_thread_host(db, **kwargs)

    def interleaved_refresh(db: HubDatabase, **kwargs: Any) -> bool:
        action()
        return refresh_thread_host(db, **kwargs)

    with (
        patch(
            "gobby.hooks.session_claimant.register_thread_host",
            side_effect=interleaved_register,
        ),
        patch(
            "gobby.hooks.session_claimant.refresh_thread_host",
            side_effect=interleaved_refresh,
        ),
    ):
        yield


def test_a_seat_that_starts_between_the_childs_claim_and_settle_keeps_its_thread(
    temp_db: HubDatabase,
) -> None:
    child = _claim(temp_db, CHILD)
    _seat_starts(temp_db)

    assert _registered(temp_db) is None
    assert _settle(temp_db, child) is None
    assert _registered(temp_db) is None


def test_a_child_settling_after_the_seats_row_exists_mints_nothing(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    child = _claim(temp_db, CHILD)
    _seat_starts(temp_db)
    seat_session = _seat_row(temp_db, session_manager, sample_project)

    assert _settle(temp_db, child) is None
    assert _registered(temp_db) is None
    owner = _mint(temp_db, owner=StartupPin(SEAT_PID, SEAT_STARTED))
    assert owner is not None
    assert owner["session_id"] == seat_session


def test_the_seats_start_drops_a_registration_the_childs_claim_wrote(
    temp_db: HubDatabase,
) -> None:
    child = _claim(temp_db, CHILD)
    assert child.proven
    assert _registered(temp_db) == WORKER

    _seat_starts(temp_db)

    assert _registered(temp_db) is None


def test_a_seat_that_pins_before_the_childs_registration_write_makes_it_withdraw(
    temp_db: HubDatabase,
) -> None:
    with _before_registration(lambda: _seat_starts(temp_db)):
        child = _claim(temp_db, CHILD)

    assert child.refusal == MISMATCH
    assert _registered(temp_db) is None


def test_a_seat_row_created_before_the_childs_registration_write_makes_it_withdraw(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    def seat_starts_and_creates_its_row() -> None:
        _seat_starts(temp_db)
        _seat_row(temp_db, session_manager, sample_project)

    with _before_registration(seat_starts_and_creates_its_row):
        child = _claim(temp_db, CHILD)

    assert child.refusal == MISMATCH
    assert _registered(temp_db) is None


def test_a_hosted_mint_names_nothing_while_a_cli_holds_the_pin(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    _seat_row(temp_db, session_manager, sample_project)
    register_thread_host(temp_db, machine_id=TEST_MACHINE_ID, external_id=THREAD, host=WORKER)
    _mark(temp_db)
    pinned = pin_startup(
        temp_db,
        machine_id=TEST_MACHINE_ID,
        source="codex",
        external_id=THREAD,
        pin=StartupPin(SEAT_PID, SEAT_STARTED),
    )
    assert pinned

    assert _mint(temp_db, host=WORKER) is None


def test_an_uncontested_child_start_still_mints_for_its_thread(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    child = _claim(temp_db, CHILD)
    session = session_manager.register(
        external_id=THREAD,
        machine_id=TEST_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
    )

    minted = _settle(temp_db, child)

    assert minted is not None
    assert minted["session_id"] == session.id
    assert _registered(temp_db) == WORKER


def test_a_seat_that_takes_a_registered_thread_before_the_refresh_keeps_it(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    register_thread_host(temp_db, machine_id=TEST_MACHINE_ID, external_id=THREAD, host=WORKER)

    def seat_starts_and_creates_its_row() -> None:
        _seat_starts(temp_db)
        _seat_row(temp_db, session_manager, sample_project)

    with _before_registration(seat_starts_and_creates_its_row):
        child = _claim(temp_db, CHILD)

    assert child.refusal == MISMATCH
    assert _registered(temp_db) is None
    assert _mint(temp_db, host=WORKER) is None


def test_a_child_settling_before_a_resumed_seat_settles_mints_nothing(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    seat_session = _seat_row(temp_db, session_manager, sample_project)
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (seat_session,))
    child = _claim(temp_db, CHILD)
    seat = _claim(temp_db, SEAT)
    # The seat's resume handler revives its row before either settle runs.
    temp_db.execute("UPDATE sessions SET status = 'active' WHERE id = %s", (seat_session,))

    assert _settle(temp_db, child) is None
    assert seat.proven


def test_a_child_claiming_after_a_resumed_seat_claims_is_refused(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    seat_session = _seat_row(temp_db, session_manager, sample_project)
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (seat_session,))
    seat = _claim(temp_db, SEAT)
    assert seat.proven

    child = _claim(temp_db, CHILD)
    temp_db.execute("UPDATE sessions SET status = 'active' WHERE id = %s", (seat_session,))

    assert child.refusal == MISMATCH
    assert _registered(temp_db) == ThreadHost(SEAT_PID, SEAT_STARTED)
    assert _mint(temp_db, host=WORKER) is None


def test_a_registered_worker_mints_nothing_once_a_seat_takes_over_its_dead_owners_row(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    seat_session = _seat_row(temp_db, session_manager, sample_project)
    dead_owner = {"parent_pid": CODEX_OLD_TUI_PID, "parent_create_time": FAKE_PROCESS_START}
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps(dead_owner), seat_session),
    )
    register_thread_host(temp_db, machine_id=TEST_MACHINE_ID, external_id=THREAD, host=WORKER)
    child = _claim(temp_db, CHILD)
    assert child.proven

    seat = _claim(temp_db, SEAT)

    assert seat.proven
    assert _settle(temp_db, child) is None
