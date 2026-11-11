"""A deferred startup's durable CLI pin, and the dead-owner recovery it allows (#25905).

Each test drives the daemon's real hook route on the test hub. Live CLIs are real
processes, so the liveness checks read the real process table: a pid plus its start time
names one process, and a recorded start time that no longer matches is a dead process.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psutil
import pytest

from gobby.hooks.envelope_dedupe import ENVELOPE_ID_HEADER
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.hooks.session_claimant import Claimant, SessionStartClaim, claim_session_start
from gobby.hooks.session_credential import SESSION_CREDENTIAL_FIELD, is_session_start_hook
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import (
    StartupPin,
    pin_startup,
    record_startup_mark,
    startup_mark,
)
from gobby.storage.sessions import SessionManager
from gobby.terminal_context import parse_terminal_context_value
from tests.hooks.test_session_credential import (
    HOOK_PID,
    TEST_MACHINE_ID,
    HookRoute,
    _fresh_workspace,
    _hook_response,
    _local_machine_identity,
    _machine_caller,
    daemon_route,
    hook_route,
)

pytestmark = pytest.mark.unit

# Fixtures imported for this module's tests.
__all__ = ["_local_machine_identity", "hook_route"]

UNPINNED = {"code": "session_startup_unpinned"}
MISMATCH = {"code": "session_claimant_mismatch"}
THREAD = "provider-session-25905-pin"


def _started(pid: int) -> float:
    return float(psutil.Process(pid).create_time())


def _mark_pinned_to(db: HubDatabase, pin: StartupPin) -> bool:
    """Leave THREAD's startup mark and pin ``pin`` to it; return whether the pin took."""
    record_startup_mark(db, machine_id=TEST_MACHINE_ID, source="claude", external_id=THREAD)
    return pin_startup(db, machine_id=TEST_MACHINE_ID, source="claude", external_id=THREAD, pin=pin)


@pytest.fixture
def victim_cli() -> Iterator[int]:
    """A live CLI process other than this test, with a real start time."""
    process = subprocess.Popen(["sleep", "300"])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()


@pytest.fixture
def dead_cli() -> StartupPin:
    """A CLI that ran and exited: its pid and the start time it had."""
    process = subprocess.Popen(["sleep", "300"])
    pin = StartupPin(cli_pid=process.pid, cli_create_time=_started(process.pid))
    process.kill()
    process.wait()
    return pin


def _claimant(pid: int, create_time: float | None = None) -> Claimant:
    started = _started(pid) if create_time is None else create_time
    return Claimant(pid=HOOK_PID, parent_pid=pid, cli_pid=pid, create_time=started)


@contextmanager
def _as(claimant: Claimant) -> Iterator[None]:
    with patch("gobby.hooks.session_claimant.resolve_claimant", return_value=claimant):
        yield


def _identity(workspace: Path, thread: str = THREAD) -> dict[str, Any]:
    return {"session_id": thread, "cwd": str(workspace), "machine_id": TEST_MACHINE_ID}


def _start(route: HookRoute, identity: dict[str, Any], start_source: str = "startup") -> Any:
    return _hook_response(
        route,
        hook_type="session-start",
        source="claude",
        input_data={**identity, "hook_event_name": "SessionStart", "source": start_source},
    )


def _prompt(
    route: HookRoute, identity: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:
    return route.client.post(
        "/api/hooks/execute",
        headers={"X-Gobby-Peer-Port": "53412", **(headers or {})},
        json={
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "critical": False,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "hook_type": "user-prompt-submit",
            "source": "claude",
            "input_data": {**identity, "hook_event_name": "UserPromptSubmit", "prompt": "go"},
        },
    )


def _row(temp_db: HubDatabase, thread: str = THREAD) -> Any:
    return temp_db.fetchone(
        "SELECT id, terminal_context FROM sessions "
        "WHERE external_id = %s AND source = 'claude' AND session_type = 'terminal'",
        (thread,),
    )


def _pin(temp_db: HubDatabase, thread: str = THREAD) -> StartupPin | None:
    mark = startup_mark(temp_db, machine_id=TEST_MACHINE_ID, source="claude", external_id=thread)
    return None if mark is None else mark.pin


def _owner(temp_db: HubDatabase, thread: str = THREAD) -> Any:
    row = _row(temp_db, thread)
    assert row is not None
    return (parse_terminal_context_value(row["terminal_context"]) or {}).get("parent_pid")


def test_after_a_restart_a_foreign_cli_can_neither_create_the_row_nor_mint(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    victim_cli: int,
) -> None:
    """H1: the startup's pin survives a daemon restart, so it still decides who owns the row."""
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    with daemon_route(temp_db) as before_restart, _as(_claimant(victim_cli)):
        _machine_caller(before_restart)
        assert _start(before_restart, identity).status_code == 200

    attacker = _claimant(os.getpid())
    with daemon_route(temp_db) as after_restart:
        _machine_caller(after_restart)
        with _as(attacker):
            foreign_prompt = _prompt(after_restart, identity)
            foreign_start = _start(after_restart, identity)
            foreign_resume = _start(after_restart, identity, start_source="resume")
        assert _row(temp_db) is None, "no foreign hook may create the pinned startup's row"
        with _as(_claimant(victim_cli)):
            own_prompt = _prompt(after_restart, identity)

    for refused in (foreign_prompt, foreign_start, foreign_resume):
        assert refused.status_code == 403
        assert refused.json() == MISMATCH
        assert SESSION_CREDENTIAL_FIELD not in refused.text
    assert own_prompt.status_code == 200, own_prompt.text
    row = _row(temp_db)
    assert row is not None
    assert own_prompt.json()[SESSION_CREDENTIAL_FIELD]["session_id"] == row["id"]
    assert _owner(temp_db) == victim_cli


def test_a_first_hook_with_no_pin_creates_no_row_and_names_why(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """H1: with no startup pin there is no row and no token, and the refusal says so."""
    _machine_caller(hook_route)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        response = _prompt(hook_route, identity)

    assert response.status_code == 403
    assert response.json() == UNPINNED
    assert _row(temp_db) is None


def test_the_first_startup_to_pin_a_thread_wins(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    victim_cli: int,
) -> None:
    _machine_caller(hook_route)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(victim_cli)):
        first = _start(hook_route, identity)
    with _as(_claimant(os.getpid())):
        second = _start(hook_route, identity)

    assert first.status_code == 200, first.text
    assert second.status_code == 403
    assert second.json() == MISMATCH
    assert _pin(temp_db) == StartupPin(victim_cli, _started(victim_cli))


def test_a_new_startup_repins_a_thread_whose_pinned_cli_died(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    dead_cli: StartupPin,
) -> None:
    _machine_caller(hook_route)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    assert _mark_pinned_to(temp_db, dead_cli)
    me = _claimant(os.getpid())

    with _as(me):
        start = _start(hook_route, identity)
        prompt = _prompt(hook_route, identity)

    assert start.status_code == 200, start.text
    assert prompt.status_code == 200, prompt.text
    row = _row(temp_db)
    assert row is not None
    assert prompt.json()[SESSION_CREDENTIAL_FIELD]["session_id"] == row["id"]
    assert _owner(temp_db) == os.getpid()


def test_a_reused_pid_with_another_start_time_is_not_the_pinned_cli(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The pinned pid now names a later process, so the pin is dead, not this CLI's."""
    _machine_caller(hook_route)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    earlier = StartupPin(cli_pid=os.getpid(), cli_create_time=_started(os.getpid()) - 3600)
    assert _mark_pinned_to(temp_db, earlier)
    me = _claimant(os.getpid())

    with _as(me):
        prompt_before = _prompt(hook_route, identity)
        start = _start(hook_route, identity)
        prompt_after = _prompt(hook_route, identity)

    # A hook never re-pins; the pinned process is gone, so only a new startup may.
    assert prompt_before.status_code == 403
    assert prompt_before.json() == MISMATCH
    assert start.status_code == 200, start.text
    assert prompt_after.status_code == 200, prompt_after.text
    assert SESSION_CREDENTIAL_FIELD in prompt_after.json()
    assert _owner(temp_db) == os.getpid()


def _live_row(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project: dict[str, Any],
    owner: StartupPin,
) -> Any:
    session = session_manager.register(
        external_id=THREAD,
        machine_id=TEST_MACHINE_ID,
        source="claude",
        project_id=project["id"],
    )
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (
            json.dumps({"parent_pid": owner.cli_pid, "parent_create_time": owner.cli_create_time}),
            session.id,
        ),
    )
    return session


def test_a_resume_from_a_new_cli_takes_over_a_dead_owners_row(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    dead_cli: StartupPin,
) -> None:
    """Crash recovery: the owner is proven dead, so a resume rebinds the row and mints."""
    _machine_caller(hook_route)
    session = _live_row(temp_db, session_manager, sample_project, dead_cli)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        resume = _start(hook_route, identity, start_source="resume")

    assert resume.status_code == 200, resume.text
    assert resume.json()[SESSION_CREDENTIAL_FIELD]["session_id"] == session.id
    assert _owner(temp_db) == os.getpid()


def test_a_resume_naming_a_live_owners_row_is_refused(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    victim_cli: int,
) -> None:
    _machine_caller(hook_route)
    _live_row(
        temp_db, session_manager, sample_project, StartupPin(victim_cli, _started(victim_cli))
    )
    identity = _identity(_fresh_workspace(tmp_path, sample_project))

    with _as(_claimant(os.getpid())):
        resume = _start(hook_route, identity, start_source="resume")

    assert resume.status_code == 403
    assert resume.json() == MISMATCH
    assert SESSION_CREDENTIAL_FIELD not in resume.text
    assert _owner(temp_db) == victim_cli


def test_two_new_clis_racing_for_a_dead_owners_row_leave_one_owner(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    dead_cli: StartupPin,
    victim_cli: int,
) -> None:
    """The row's owner is swapped by compare-and-swap, so the second claim finds a live owner."""
    _live_row(temp_db, session_manager, sample_project, dead_cli)

    def claim(pid: int) -> SessionStartClaim:
        return claim_session_start(
            temp_db,
            claimant=_claimant(pid),
            daemon_pid=-1,
            external_id=THREAD,
            source="claude",
            machine_id=TEST_MACHINE_ID,
            start=True,
        )

    first = claim(os.getpid())
    second = claim(victim_cli)

    assert first.refusal is None and first.proven
    assert second.refusal == MISMATCH["code"]
    assert _owner(temp_db) == os.getpid()


def test_a_live_row_that_names_its_cli_by_pid_alone_proves_no_claimant(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    victim_cli: int,
) -> None:
    """Any process holding a reused pid matches a bare pid, so the claim needs its start time."""
    session = _live_row(
        temp_db, session_manager, sample_project, StartupPin(victim_cli, _started(victim_cli))
    )
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps({"parent_pid": victim_cli}), session.id),
    )

    claim = claim_session_start(
        temp_db,
        claimant=_claimant(victim_cli),
        daemon_pid=-1,
        external_id=THREAD,
        source="claude",
        machine_id=TEST_MACHINE_ID,
        start=True,
    )

    assert not claim.proven
    assert claim.refusal == MISMATCH["code"]
    assert _owner(temp_db) == victim_cli


def test_a_daemon_replay_never_stores_the_pid_its_body_names(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    victim_cli: int,
) -> None:
    """An unproven hook's body pid never becomes the row's owner, so it cannot mint later."""
    _machine_caller(hook_route)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    replay = Claimant(pid=os.getpid(), parent_pid=None, cli_pid=None)

    with _as(replay):
        replayed = _prompt(hook_route, {**identity, "terminal_context": {"parent_pid": victim_cli}})
    with _as(_claimant(victim_cli)):
        start = _start(hook_route, identity, start_source="resume")

    assert replayed.status_code == 200, replayed.text
    assert _owner(temp_db) is None
    # The ownerless row's hooks still run; no CLI can ever mint for it.
    assert start.status_code == 200, start.text
    assert SESSION_CREDENTIAL_FIELD not in start.text
    assert _owner(temp_db) is None


def test_the_replay_of_a_refused_hook_gives_the_row_to_the_pinned_cli(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    victim_cli: int,
) -> None:
    """ghook keeps a refused envelope and the drain replays it; the row stays the victim's.

    The replay still runs the hook, so a thread's hooks are never lost, but the row it
    creates names the CLI the startup pinned and not the body's pid, so only that CLI mints.
    """
    _machine_caller(hook_route)
    identity = _identity(_fresh_workspace(tmp_path, sample_project))
    with _as(_claimant(victim_cli)):
        _start(hook_route, identity)
    envelope = {ENVELOPE_ID_HEADER: "0198f000-0000-7000-8000-000000025905"}
    attacker = _claimant(os.getpid())
    body = {**identity, "terminal_context": {"parent_pid": os.getpid()}}

    with _as(attacker):
        refused = _prompt(hook_route, body, headers=envelope)
    with _as(Claimant(pid=os.getpid(), parent_pid=None, cli_pid=None)):
        replayed = _prompt(hook_route, body, headers=envelope)
    with _as(attacker):
        foreign_resume = _start(hook_route, identity, start_source="resume")
    with _as(_claimant(victim_cli)):
        own_resume = _start(hook_route, identity, start_source="resume")

    assert refused.status_code == 403
    assert replayed.status_code == 200, replayed.text
    assert SESSION_CREDENTIAL_FIELD not in replayed.text
    assert _owner(temp_db) == victim_cli
    assert foreign_resume.status_code == 403
    assert own_resume.status_code == 200, own_resume.text
    assert SESSION_CREDENTIAL_FIELD in own_resume.json()


AGY_THREAD = "provider-session-25916-agy"


def _agy_invocation(route: HookRoute, identity: dict[str, Any]) -> Any:
    return _hook_response(
        route,
        hook_type="PreInvocation",
        source="agy",
        input_data={**identity, "hook_event_name": "PreInvocation", "prompt": "go"},
    )


@pytest.mark.parametrize(
    ("source", "hook_type", "starts"),
    [
        ("agy", "PreInvocation", True),
        ("agy", "PostInvocation", False),
        ("agy", "PreToolUse", False),
        ("agy", "Stop", False),
        ("claude", "session-start", True),
        ("claude", "user-prompt-submit", False),
        ("codex", "SessionStart", True),
    ],
)
def test_the_provider_decides_which_hook_starts_its_session(
    source: str, hook_type: str, starts: bool
) -> None:
    assert is_session_start_hook(source, hook_type) is starts


def test_an_antigravity_first_invocation_starts_and_pins_its_session(
    hook_route: HookRoute,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    victim_cli: int,
) -> None:
    """Antigravity has no SessionStart hook: its first PreInvocation is the start."""
    _machine_caller(hook_route)
    identity = _identity(_fresh_workspace(tmp_path, sample_project), thread=AGY_THREAD)

    with _as(_claimant(victim_cli)):
        first = _agy_invocation(hook_route, identity)
        next_turn = _agy_invocation(hook_route, identity)
    with _as(_claimant(os.getpid())):
        foreign = _agy_invocation(hook_route, identity)

    assert first.status_code == 200, first.text
    assert next_turn.status_code == 200, next_turn.text
    assert foreign.status_code == 403
    assert foreign.json() == MISMATCH
    row = temp_db.fetchone(
        "SELECT terminal_context FROM sessions "
        "WHERE external_id = %s AND source = 'agy' AND session_type = 'terminal'",
        (AGY_THREAD,),
    )
    assert row is not None
    context = parse_terminal_context_value(row["terminal_context"])
    assert context is not None
    assert context["parent_pid"] == victim_cli
