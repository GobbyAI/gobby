"""Tests for handoff identity matching."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.sessions.handoff_identity import (
    sessions_have_continuous_terminal_context,
    terminal_context_matches_session,
    terminal_contexts_match,
    terminal_process_contexts_match,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("first", "second", "matches"),
    [
        ({"gobby_terminal_id": "native-a"}, {"gobby_terminal_id": "native-a"}, True),
        ({"gobby_terminal_id": "native-a"}, {"gobby_terminal_id": "native-b"}, False),
        ({"gobby_terminal_id": ""}, {"gobby_terminal_id": ""}, False),
        ({"gobby_terminal_id": 42}, {"gobby_terminal_id": 42}, False),
        (
            {"gobby_terminal_id": "native-a", "tty": "/dev/ttys1"},
            {"gobby_terminal_id": "native-b", "tty": "/dev/ttys1"},
            True,
        ),
    ],
)
def test_terminal_contexts_match_same_native_terminal(
    first: dict[str, str | int], second: dict[str, str | int], matches: bool
) -> None:
    assert terminal_contexts_match(first, second) is matches


def test_terminal_process_contexts_require_exact_process_birth_identity() -> None:
    canonical = {
        "gobby_terminal_id": "native-a",
        "parent_pid": 1234,
        "parent_create_time": 5678.25,
    }

    assert terminal_process_contexts_match(canonical, dict(canonical))
    assert not terminal_process_contexts_match(
        canonical,
        {**canonical, "parent_create_time": 5679.25},
    )
    assert not terminal_process_contexts_match(
        canonical,
        {key: value for key, value in canonical.items() if key != "parent_create_time"},
    )


def test_terminal_context_matches_session_by_gobby_session_id() -> None:
    session = MagicMock()
    session.id = "session-5815"
    session.terminal_context = {"gobby_terminal_id": "%1"}

    assert terminal_context_matches_session(
        session,
        {"gobby_session_id": "session-5815", "gobby_terminal_id": "%99"},
    )


def test_sessions_have_continuous_terminal_context_rejects_different_native_ids() -> None:
    parent = MagicMock()
    parent.id = "session-5815"
    parent.terminal_context = {"gobby_terminal_id": "%5815"}
    child = MagicMock()
    child.id = "session-5867"
    child.terminal_context = {"gobby_terminal_id": "%5867"}

    assert not sessions_have_continuous_terminal_context(parent, child)
