"""Tests for handoff identity matching."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.sessions.handoff_identity import (
    sessions_have_continuous_terminal_context,
    terminal_context_matches_session,
    terminal_contexts_match,
)

pytestmark = pytest.mark.unit


def test_terminal_contexts_match_same_tmux_pane_and_socket() -> None:
    assert terminal_contexts_match(
        {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
        {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
    )


def test_terminal_contexts_reject_same_pane_on_different_tmux_socket() -> None:
    assert not terminal_contexts_match(
        {"tmux_pane": "%12", "tmux_socket_path": "/tmp/old"},
        {"tmux_pane": "%12", "tmux_socket_path": "/tmp/new"},
    )


def test_terminal_context_matches_session_by_gobby_session_id() -> None:
    session = MagicMock()
    session.id = "session-5815"
    session.terminal_context = {"tmux_pane": "%1"}

    assert terminal_context_matches_session(
        session,
        {"gobby_session_id": "session-5815", "tmux_pane": "%99"},
    )


def test_sessions_have_continuous_terminal_context_rejects_different_tmux_panes() -> None:
    parent = MagicMock()
    parent.id = "session-5815"
    parent.terminal_context = {"tmux_pane": "%5815", "tmux_socket_path": "/tmp/tmux"}
    child = MagicMock()
    child.id = "session-5867"
    child.terminal_context = {"tmux_pane": "%5867", "tmux_socket_path": "/tmp/tmux"}

    assert not sessions_have_continuous_terminal_context(parent, child)
