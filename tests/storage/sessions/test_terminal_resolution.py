"""Tests for resolving the current terminal session from ambient context."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.hooks.event_handlers._session_start.terminal_runtime import bind_discovered_terminal
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("prebound", [False, True], ids=["context-fallback", "previously-bound"])
def test_tty_mismatch_session_resolves_no_terminal(
    session_manager: SessionManager, sample_project: dict[str, Any], prebound: bool
) -> None:
    terminals = TerminalManager(session_manager.db)
    terminal_id = str(uuid4())
    terminal = terminals.create_pending(
        terminal_id=terminal_id,
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
    )
    assert terminal.attempt_started_at is not None
    assert (
        terminals.record_process(
            terminal_id,
            {"pgid": 101, "host_terminal_id": terminal_id},
            attempt_generation=terminal.attempt_generation,
            attempt_started_at=terminal.attempt_started_at,
        )
        is not None
    )
    context = {"gobby_terminal_id": terminal_id, "parent_pid": 202, "tty": "/dev/ttys002"}
    session = _register_terminal(
        session_manager, sample_project["id"], "tty-mismatch", terminal_context=context
    )
    if prebound:
        assert terminals.bind_session(terminal_id, session.id, session.project_id) is not None
    assert terminals.resolve_live_for_session(session) is not None
    processes = {
        101: SimpleNamespace(terminal=lambda: "/dev/ttys001"),
        202: SimpleNamespace(terminal=lambda: "/dev/ttys002"),
    }
    with patch("gobby.sessions.terminal_binding.psutil.Process", side_effect=processes.__getitem__):
        assert (
            bind_discovered_terminal(
                terminals,
                logging.getLogger(__name__),
                session_id=session.id,
                project_id=session.project_id,
                terminal_context=context,
                session_manager=session_manager,
            )
            is None
        )
    stored = session_manager.get(session.id)
    assert stored is not None
    assert stored.terminal_context == {"parent_pid": 202, "tty": "/dev/ttys002"}
    assert terminals.resolve_live_for_session(stored) is None
    unchanged = terminals.get(terminal_id)
    assert unchanged is not None and unchanged.session_id is None


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _register_terminal(
    session_manager: SessionManager,
    project_id: str,
    external_id: str,
    *,
    status: str = "active",
    session_type: str = "terminal",
    terminal_context: dict[str, Any] | None = None,
) -> Session:
    session = session_manager.register(
        external_id=external_id,
        machine_id="20000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=project_id,
        session_type=session_type,
        terminal_context=terminal_context,
    )
    if status == "active":
        return session

    updated = session_manager.update_status(session.id, status)
    assert updated is not None
    return updated


def test_resolve_current_terminal_session_prefers_active_status(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    active = _register_terminal(
        session_manager,
        sample_project["id"],
        "active-lower-score",
        terminal_context={
            "parent_pid": 4242,
            "gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68",
        },
    )
    _register_terminal(
        session_manager,
        sample_project["id"],
        "paused-higher-score",
        status="paused",
        terminal_context={
            "parent_pid": 4242,
            "gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68",
        },
    )

    resolved = session_manager.resolve_current_terminal_session(
        sample_project["id"],
        4242,
        {"gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68"},
    )

    assert resolved is not None
    assert resolved.id == active.id


def test_resolve_current_terminal_session_uses_best_inactive_fallback(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    _register_terminal(
        session_manager,
        sample_project["id"],
        "paused-pane-only",
        status="paused",
        terminal_context={
            "parent_pid": 9000,
            "gobby_terminal_id": "1c11aee4-7819-5bf9-b7c2-c626a4531578",
        },
    )
    handoff = _register_terminal(
        session_manager,
        sample_project["id"],
        "handoff-pid-and-pane",
        status="awaiting_handoff",
        terminal_context={
            "parent_pid": 4242,
            "gobby_terminal_id": "1c11aee4-7819-5bf9-b7c2-c626a4531578",
        },
    )

    resolved = session_manager.resolve_current_terminal_session(
        sample_project["id"],
        4242,
        {"gobby_terminal_id": "1c11aee4-7819-5bf9-b7c2-c626a4531578"},
    )

    assert resolved is not None
    assert resolved.id == handoff.id


def test_resolve_current_terminal_session_rejects_equal_top_scores(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    for suffix in ("one", "two"):
        _register_terminal(
            session_manager,
            sample_project["id"],
            f"active-tie-{suffix}",
            terminal_context={
                "parent_pid": 4242,
                "gobby_terminal_id": "a07635eb-e9c1-523f-81af-071a764ef106",
            },
        )

    assert (
        session_manager.resolve_current_terminal_session(
            sample_project["id"],
            4242,
            {"gobby_terminal_id": "a07635eb-e9c1-523f-81af-071a764ef106"},
        )
        is None
    )


@pytest.mark.parametrize(
    ("project_id", "parent_pid", "terminal_context"),
    [
        (None, 4242, {"gobby_terminal_id": "b4127335-e8bc-5be8-ab12-f2d057d48577"}),
        ("", 4242, {"gobby_terminal_id": "b4127335-e8bc-5be8-ab12-f2d057d48577"}),
        ("project", None, None),
        ("project", 0, {}),
    ],
)
def test_resolve_current_terminal_session_rejects_missing_identity(
    session_manager: SessionManager,
    project_id: str | None,
    parent_pid: Any,
    terminal_context: dict[str, Any] | None,
) -> None:
    assert (
        session_manager.resolve_current_terminal_session(
            project_id,
            parent_pid,
            terminal_context,
        )
        is None
    )


def test_resolve_current_terminal_session_scopes_identical_fingerprints_by_project(
    session_manager: SessionManager,
    sample_project: dict[str, str],
    project_manager: LocalProjectManager,
) -> None:
    terminal_context = {
        "parent_pid": 4242,
        "gobby_terminal_id": "eb5fbd30-6279-5e62-b0aa-609362ddc54b",
    }
    expected = _register_terminal(
        session_manager,
        sample_project["id"],
        "requested-project",
        terminal_context=terminal_context,
    )
    other_project = project_manager.create("other-terminal-resolution-project")
    _register_terminal(
        session_manager,
        other_project.id,
        "other-project",
        terminal_context=terminal_context,
    )

    resolved = session_manager.resolve_current_terminal_session(
        sample_project["id"],
        4242,
        terminal_context,
    )

    assert resolved is not None
    assert resolved.id == expected.id


@pytest.mark.parametrize(
    ("status", "session_type"),
    [
        ("expired", "terminal"),
        ("deleted", "terminal"),
        ("active", "web_chat"),
    ],
)
def test_resolve_current_terminal_session_excludes_ineligible_rows(
    session_manager: SessionManager,
    sample_project: dict[str, str],
    status: str,
    session_type: str,
) -> None:
    terminal_context = {
        "parent_pid": 4242,
        "gobby_terminal_id": "ba1779a2-8033-5de2-b679-4eb364c792ed",
    }
    _register_terminal(
        session_manager,
        sample_project["id"],
        f"ineligible-{status}-{session_type}",
        status=status,
        session_type=session_type,
        terminal_context=terminal_context,
    )

    resolved = session_manager.resolve_current_terminal_session(
        sample_project["id"],
        4242,
        terminal_context,
    )

    assert resolved is None
