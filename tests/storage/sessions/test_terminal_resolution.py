"""Tests for resolving the current terminal session from ambient context."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager


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
        machine_id="machine",
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
        terminal_context={"parent_pid": 4242, "tmux_pane": "%1"},
    )
    _register_terminal(
        session_manager,
        sample_project["id"],
        "paused-higher-score",
        status="paused",
        terminal_context={
            "parent_pid": 4242,
            "tmux_pane": "%1",
            "tmux_socket_path": "/tmp/tmux/default",
        },
    )

    resolved = session_manager.resolve_current_terminal_session(
        sample_project["id"],
        4242,
        {"tmux_pane": "%1", "tmux_socket_path": "/tmp/tmux/default"},
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
        terminal_context={"parent_pid": 9000, "tmux_pane": "%2"},
    )
    handoff = _register_terminal(
        session_manager,
        sample_project["id"],
        "handoff-pid-and-pane",
        status="handoff_ready",
        terminal_context={"parent_pid": 4242, "tmux_pane": "%2"},
    )

    resolved = session_manager.resolve_current_terminal_session(
        sample_project["id"],
        4242,
        {"tmux_pane": "%2"},
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
            terminal_context={"parent_pid": 4242, "tmux_pane": "%3"},
        )

    assert (
        session_manager.resolve_current_terminal_session(
            sample_project["id"],
            4242,
            {"tmux_pane": "%3"},
        )
        is None
    )


@pytest.mark.parametrize(
    ("project_id", "parent_pid", "terminal_context"),
    [
        (None, 4242, {"tmux_pane": "%4"}),
        ("", 4242, {"tmux_pane": "%4"}),
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


def test_resolve_current_terminal_session_rejects_cross_socket_pane_collision(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    _register_terminal(
        session_manager,
        sample_project["id"],
        "other-socket",
        terminal_context={
            "parent_pid": 4242,
            "tmux_pane": "%5",
            "tmux_socket_path": "/tmp/tmux/gobby",
        },
    )

    resolved = session_manager.resolve_current_terminal_session(
        sample_project["id"],
        4242,
        {"tmux_pane": "%5", "tmux_socket_path": "/tmp/tmux/default"},
    )

    assert resolved is None


def test_resolve_current_terminal_session_scopes_identical_fingerprints_by_project(
    session_manager: SessionManager,
    sample_project: dict[str, str],
    project_manager: LocalProjectManager,
) -> None:
    terminal_context = {
        "parent_pid": 4242,
        "tmux_pane": "%6",
        "tmux_socket_path": "/tmp/tmux/default",
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
    terminal_context = {"parent_pid": 4242, "tmux_pane": "%7"}
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
