"""Database ownership of Codex seat TUIs for first-activity adoption (#23032)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks.codex_seat import (
    codex_clear_activity_search,
    codex_seat_available,
    codex_seat_owner,
    codex_thread_originator,
)
from gobby.hooks.event_handlers._session_start.terminal_runtime import (
    discover_and_bind_external_terminal,
)
from gobby.hooks.terminal_context import (
    SeatOwner,
    enrich_terminal_context_with_cwd,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.terminal_context import CODEX_CLEAR_ORIGIN_KEY
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


def test_a_recorded_owner_outranks_an_unbound_thread(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    """#25647: a clear successor adopts only a seat whose owner is recorded."""
    started = time.time() - 300
    owner_of = codex_seat_owner(session_manager.db, LOCAL_MACHINE_ID)
    assert owner_of(SEAT_PID, started) is SeatOwner.NONE

    _register(session_manager, sample_project["id"], {"cwd": "/repo"})
    assert owner_of(SEAT_PID, started) is SeatOwner.POSSIBLE

    _register(
        session_manager,
        sample_project["id"],
        {"cwd": "/repo", "parent_pid": SEAT_PID, "parent_create_time": started},
        external_id="01a0e8d5-0000-7000-8000-000000000002",
    )
    assert owner_of(SEAT_PID, started) is SeatOwner.RECORDED


THREAD_ID = "01a1a561-0000-7000-8000-000000025647"


@pytest.mark.parametrize(
    ("first_line", "expected"),
    [
        (
            {"type": "session_meta", "payload": {"id": THREAD_ID, "originator": "codex-tui"}},
            "codex-tui",
        ),
        (
            {
                "type": "session_meta",
                "payload": {"id": "another-thread", "originator": "codex-tui"},
            },
            None,
        ),
        ({"type": "session_meta", "payload": {"id": THREAD_ID}}, None),
        ({"type": "turn_context", "payload": {"id": THREAD_ID, "originator": "codex-tui"}}, None),
        ("not json", None),
    ],
    ids=["tui", "other-thread", "no-originator", "not-session-meta", "malformed"],
)
def test_thread_originator_reads_only_its_own_rollout_header(
    tmp_path: Path, first_line: dict[str, Any] | str, expected: str | None
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    text = first_line if isinstance(first_line, str) else json.dumps(first_line)
    rollout.write_text(text + "\n")

    assert codex_thread_originator(str(rollout), THREAD_ID) == expected


def test_thread_originator_is_unknown_without_a_readable_rollout(tmp_path: Path) -> None:
    assert codex_thread_originator(None, THREAD_ID) is None
    assert codex_thread_originator(str(tmp_path / "missing.jsonl"), THREAD_ID) is None
    assert codex_thread_originator(str(tmp_path), THREAD_ID) is None


@pytest.mark.parametrize(
    ("recorded", "rollout_originator", "expected"),
    [
        ({}, "codex-tui", None),
        ({CODEX_CLEAR_ORIGIN_KEY: "codex-tui"}, "codex-tui", None),
        ({CODEX_CLEAR_ORIGIN_KEY: "codex_vscode"}, "codex-tui", (False, False, "codex_vscode")),
        ({CODEX_CLEAR_ORIGIN_KEY: "pending", "parent_pid": SEAT_PID}, "codex-tui", None),
        ({CODEX_CLEAR_ORIGIN_KEY: "pending"}, None, (False, False, "pending")),
        ({CODEX_CLEAR_ORIGIN_KEY: "pending"}, "codex_vscode", (False, False, "codex_vscode")),
        ({CODEX_CLEAR_ORIGIN_KEY: "pending"}, "codex-tui", (True, True, "codex-tui")),
    ],
    ids=[
        "no-clear",
        "tui-settled",
        "other-client-settled",
        "pending-with-seat",
        "pending-no-rollout",
        "pending-other-client",
        "pending-tui",
    ],
)
def test_clear_activity_search_rechecks_only_a_pending_origin(
    session_manager: SessionManager,
    tmp_path: Path,
    recorded: dict[str, Any],
    rollout_originator: str | None,
    expected: tuple[bool, bool, str] | None,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    if rollout_originator is not None:
        meta = {"id": THREAD_ID, "originator": rollout_originator}
        rollout.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n")

    search = codex_clear_activity_search(
        session_manager.db, LOCAL_MACHINE_ID, recorded, str(rollout), THREAD_ID
    )

    if expected is None:
        assert search is None
        return
    assert search is not None
    searches, has_owner, origin = expected
    assert (search.thread_id == THREAD_ID) is searches
    assert (search.thread_id is None) is not searches
    assert (search.owner is not None) is has_owner
    assert search.origin == origin


def test_hook_payload_cannot_carry_a_clear_origin() -> None:
    enriched = enrich_terminal_context_with_cwd(
        {CODEX_CLEAR_ORIGIN_KEY: "pending", "tty": "/dev/ttys004"}, "/repo"
    )
    assert enriched == {"tty": "/dev/ttys004", "cwd": "/repo"}


@pytest.mark.timeout(10)
def test_thread_originator_never_waits_on_a_pipe(tmp_path: Path) -> None:
    pipe = tmp_path / "rollout.jsonl"
    os.mkfifo(pipe)
    assert codex_thread_originator(str(pipe), THREAD_ID) is None


def test_thread_originator_never_follows_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    meta = {"id": THREAD_ID, "originator": "codex-tui"}
    target.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
    link = tmp_path / "rollout.jsonl"
    link.symlink_to(target)

    assert codex_thread_originator(str(target), THREAD_ID) == "codex-tui"
    assert codex_thread_originator(str(link), THREAD_ID) is None


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


def test_a_new_human_codex_thread_binds_the_gterm_pane_it_names(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    terminals = TerminalManager(temp_db)
    pane = str(uuid.uuid4())
    terminals.create_pending(
        terminal_id=pane,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=pane,
    )
    terminal_context = {
        "cwd": "/repo",
        "gobby_terminal_id": pane,
        "parent_pid": SEAT_PID,
        "parent_create_time": time.time() - 5,
    }
    session_id = _register(session_manager, project_id, terminal_context)
    handler = SimpleNamespace(terminal_manager=terminals, logger=logging.getLogger(__name__))

    pending = discover_and_bind_external_terminal(
        handler, session_id=session_id, project_id=project_id, terminal_context=terminal_context
    )

    assert pending is None
    bound = terminals.get_live_for_session(session_id)
    assert bound is not None
    assert bound.id == pane
