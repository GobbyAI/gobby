"""An operator session gets its credential back on its next hook (#26072).

A session token lasts 24 hours and the sweeper expires a row idle that long, so a quiet
session loses both. Its CLI process is still the row's recorded owner. The next hook that
process sends with the machine key revives the row and carries a fresh credential; no
other process gains anything.

Each test drives the daemon's real hook route on the test hub, and rows are expired by
the real sweeper. The live-row case, where only the token ran out, is
``test_an_attested_activity_hook_on_a_live_session_carries_its_credential`` in
``test_session_credential``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.hooks.session_claimant import Claimant, claim_session_start
from gobby.hooks.session_credential import SESSION_CREDENTIAL_FIELD
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import StartupPin
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.hooks.test_session_credential import (
    TEST_MACHINE_ID,
    HookRoute,
    _fresh_workspace,
    _local_machine_identity,
    _machine_caller,
    hook_route,
)
from tests.hooks.test_session_startup_pins import (
    MISMATCH,
    THREAD,
    UNPINNED,
    _as,
    _claimant,
    _identity,
    _live_row,
    _owner,
    _prompt,
    _start,
    _started,
    victim_cli,
)

pytestmark = pytest.mark.unit

# Fixtures imported for this module's tests.
__all__ = ["_local_machine_identity", "hook_route", "victim_cli"]

# Revival by plain hook activity stops this long after a row expired.
PAST_HORIZON_HOURS = 30


def _me() -> StartupPin:
    return StartupPin(cli_pid=os.getpid(), cli_create_time=_started(os.getpid()))


def _status(temp_db: HubDatabase, session_id: str) -> str:
    row = temp_db.fetchone("SELECT status FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    return str(row["status"])


def _expire(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    session_id: str,
    *,
    hours_ago: int = 0,
) -> None:
    """Expire the row of a CLI that is still running, then age that expiry by ``hours_ago``.

    The idle sweeper leaves such a row alone, so this is the liveness monitor's expiry:
    the one a running CLI gets when it is no longer its terminal's foreground process.
    """
    row = temp_db.fetchone(
        "SELECT machine_id, updated_at FROM sessions WHERE id = %s", (session_id,)
    )
    assert row is not None
    expired = session_manager.expire_if_active(
        session_id, machine_id=row["machine_id"], observed_updated_at=row["updated_at"]
    )
    assert expired is not None
    if hours_ago:
        temp_db.execute(
            "UPDATE sessions SET updated_at = NOW() - make_interval(hours => %s) WHERE id = %s",
            (hours_ago, session_id),
        )
    assert _status(temp_db, session_id) == "expired"


def _conversation(
    session_manager: SessionManager,
    project: dict[str, Any],
    thread: str,
    *,
    parent: str | None = None,
) -> Any:
    """A live row for another conversation held by this same CLI process."""
    me = _me()
    return session_manager.register(
        external_id=thread,
        machine_id=TEST_MACHINE_ID,
        source="claude",
        project_id=project["id"],
        parent_session_id=parent,
        terminal_context={"parent_pid": me.cli_pid, "parent_create_time": me.cli_create_time},
    )


def _session_end(route: HookRoute, identity: dict[str, Any]) -> Any:
    return route.client.post(
        "/api/hooks/execute",
        headers={"X-Gobby-Peer-Port": "53412"},
        json={
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "critical": False,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "hook_type": "session-end",
            "source": "claude",
            "input_data": {**identity, "hook_event_name": "SessionEnd", "reason": "other"},
        },
    )


@pytest.mark.parametrize(
    "hours_ago",
    [0, PAST_HORIZON_HOURS, 24 * 14],
    ids=["just-expired", "past-the-activity-horizon", "two-weeks"],
)
def test_the_owning_clis_hook_revives_its_expired_row_and_carries_a_credential(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    hours_ago: int,
) -> None:
    """However long the session sat idle, its own CLI's next hook brings it back."""
    _machine_caller(hook_route)
    session = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, session.id, hours_ago=hours_ago)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        prompt = _prompt(hook_route, identity)

    assert prompt.status_code == 200, prompt.text
    assert prompt.json()[SESSION_CREDENTIAL_FIELD]["session_id"] == session.id
    assert _status(temp_db, session.id) == "active"


def test_another_clis_hook_leaves_the_expired_row_expired(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    victim_cli: int,
) -> None:
    """Naming the thread is no proof: only the recorded owner process revives the row."""
    _machine_caller(hook_route)
    session = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, session.id)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(victim_cli)):
        prompt = _prompt(hook_route, identity)

    assert prompt.status_code == 403
    assert prompt.json() == UNPINNED
    assert _status(temp_db, session.id) == "expired"


def test_a_reused_pid_with_another_start_time_does_not_revive_the_row(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The owner's pid now names a later process, which is not the owner."""
    _machine_caller(hook_route)
    earlier = StartupPin(cli_pid=os.getpid(), cli_create_time=_started(os.getpid()) - 3600)
    session = _live_row(temp_db, session_manager, sample_project, earlier)
    _expire(temp_db, session_manager, session.id)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        prompt = _prompt(hook_route, identity)

    assert prompt.status_code == 403
    assert prompt.json() == UNPINNED
    assert _status(temp_db, session.id) == "expired"


def test_a_cleared_sessions_late_hook_does_not_bring_it_back_beside_its_successor(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The same CLI cleared this session: its successor stays the pane's only live row."""
    _machine_caller(hook_route)
    session = _live_row(temp_db, session_manager, sample_project, _me())
    successor = session_manager.register(
        external_id="provider-session-26072-successor",
        machine_id=TEST_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    SessionVariableManager(temp_db).merge_variables(
        session.id, {CLEAR_ATTEMPT_VARIABLE: {"consumed_by": successor.id}}
    )
    _expire(temp_db, session_manager, session.id)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        prompt = _prompt(hook_route, identity)

    assert prompt.status_code == 403
    assert prompt.json() == UNPINNED
    assert _status(temp_db, session.id) == "expired"
    assert _status(temp_db, successor.id) == "active"


def test_the_owning_clis_session_end_does_not_revive_its_expired_row(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A CLI that is leaving has nothing to bring back."""
    _machine_caller(hook_route)
    session = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, session.id, hours_ago=PAST_HORIZON_HOURS)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        ended = _session_end(hook_route, identity)

    assert ended.status_code == 403
    assert ended.json() == UNPINNED
    assert _status(temp_db, session.id) == "expired"


def test_a_cli_that_moved_to_another_conversation_does_not_revive_the_one_it_left(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """One CLI holds one conversation: a late hook for the old one changes nothing."""
    _machine_caller(hook_route)
    left = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, left.id)
    current = _conversation(session_manager, sample_project, "provider-session-26072-current")
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        prompt = _prompt(hook_route, identity)

    assert prompt.status_code == 403
    assert prompt.json() == UNPINNED
    assert _status(temp_db, left.id) == "expired"
    assert _status(temp_db, current.id) == "active"


def test_a_twice_cleared_sessions_late_hook_does_not_bring_it_back(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The first session's own successor is gone too, so only the CLI's live row shows it left."""
    _machine_caller(hook_route)
    first = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, first.id)
    second = _conversation(
        session_manager, sample_project, "provider-session-26072-second", parent=first.id
    )
    _expire(temp_db, session_manager, second.id)
    third = _conversation(
        session_manager, sample_project, "provider-session-26072-third", parent=second.id
    )
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        prompt = _prompt(hook_route, identity)

    assert prompt.status_code == 403
    assert prompt.json() == UNPINNED
    assert _status(temp_db, first.id) == "expired"
    assert _status(temp_db, third.id) == "active"


def test_a_row_another_cli_registered_while_it_came_back_refuses_its_former_owner(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    victim_cli: int,
) -> None:
    """Another CLI's resume can register the row between the owner check and the revival."""
    session = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, session.id)

    def registered_by_another_cli(session_id: str) -> bool:
        temp_db.execute(
            "UPDATE sessions SET status = 'active', terminal_context = %s::jsonb WHERE id = %s",
            (
                json.dumps({"parent_pid": victim_cli, "parent_create_time": _started(victim_cli)}),
                session_id,
            ),
        )
        return True

    claim = claim_session_start(
        temp_db,
        claimant=_claimant(os.getpid()),
        daemon_pid=-1,
        external_id=THREAD,
        source="claude",
        machine_id=TEST_MACHINE_ID,
        start=False,
        revive=registered_by_another_cli,
    )

    assert not claim.proven
    assert claim.refusal == MISMATCH["code"]
    assert _owner(temp_db) == victim_cli


@pytest.mark.parametrize(
    "stored_owner",
    [{}, {"parent_pid": os.getpid()}],
    ids=["no-owner", "pid-without-a-start-time"],
)
def test_an_expired_row_that_names_no_process_is_not_revived(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    stored_owner: dict[str, Any],
) -> None:
    """A reused pid matches a bare pid, so the row must store the process's start time too."""
    _machine_caller(hook_route)
    session = _live_row(temp_db, session_manager, sample_project, _me())
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps(stored_owner), session.id),
    )
    _expire(temp_db, session_manager, session.id)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        prompt = _prompt(hook_route, identity)

    assert prompt.status_code == 403
    assert prompt.json() == UNPINNED
    assert _status(temp_db, session.id) == "expired"


def test_a_daemon_replay_for_an_expired_row_carries_no_credential(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A replayed envelope comes from the daemon, which is no session's CLI."""
    _machine_caller(hook_route)
    session = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, session.id, hours_ago=PAST_HORIZON_HOURS)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    replay = Claimant(pid=os.getpid(), parent_pid=None, cli_pid=None)

    with _as(replay):
        replayed = _prompt(hook_route, identity)

    # Its prompt registers the session again, as before; registering is no proof of a CLI.
    assert replayed.status_code == 200, replayed.text
    assert SESSION_CREDENTIAL_FIELD not in replayed.text
    assert _owner(temp_db) == os.getpid()


def test_a_daemon_replay_does_not_revive_a_conversation_its_cli_left(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """#26073: a replay is old news, and the CLI's live row shows what it holds now."""
    _machine_caller(hook_route)
    left = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, left.id)
    current = _conversation(session_manager, sample_project, "provider-session-26073-current")
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    replay = Claimant(pid=os.getpid(), parent_pid=None, cli_pid=None)

    with _as(replay):
        replayed = _prompt(hook_route, identity)

    assert replayed.status_code == 200, replayed.text
    assert SESSION_CREDENTIAL_FIELD not in replayed.text
    assert _status(temp_db, left.id) == "expired"
    assert _status(temp_db, current.id) == "active"


def test_a_daemon_replay_does_not_revive_a_twice_cleared_conversation(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """#26073: the first session's own successor is gone, so no live child names it cleared."""
    _machine_caller(hook_route)
    first = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, first.id)
    second = _conversation(
        session_manager, sample_project, "provider-session-26073-second", parent=first.id
    )
    _expire(temp_db, session_manager, second.id)
    third = _conversation(
        session_manager, sample_project, "provider-session-26073-third", parent=second.id
    )
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    replay = Claimant(pid=os.getpid(), parent_pid=None, cli_pid=None)

    with _as(replay):
        replayed = _prompt(hook_route, identity)

    assert replayed.status_code == 200, replayed.text
    assert SESSION_CREDENTIAL_FIELD not in replayed.text
    assert _status(temp_db, first.id) == "expired"
    assert _status(temp_db, second.id) == "expired"
    assert _status(temp_db, third.id) == "active"


def test_a_cli_that_resumes_a_cleared_conversation_gets_it_back(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A start is the CLI saying where it is now, so its live successor row gives way."""
    _machine_caller(hook_route)
    cleared = _live_row(temp_db, session_manager, sample_project, _me())
    _expire(temp_db, session_manager, cleared.id)
    successor = _conversation(
        session_manager, sample_project, "provider-session-26073-successor", parent=cleared.id
    )
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        resumed = _start(hook_route, identity, "resume")

    assert resumed.status_code == 200, resumed.text
    assert _status(temp_db, cleared.id) == "active"
    assert _status(temp_db, successor.id) == "expired"
