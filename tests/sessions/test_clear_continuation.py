"""Clear-successor continuation scheduling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.sessions.clear_continuation import schedule_handoff_continuation

pytestmark = pytest.mark.unit


def test_schedule_handoff_continuation_forwards_runtime_and_fallback() -> None:
    """The clear pull routes like compaction: live terminals row first, then tmux."""
    session = SimpleNamespace(
        id="succ-1", source="claude", terminal_context={"gobby_terminal_id": "term-1"}
    )
    db = MagicMock(name="db")
    terminal_manager = MagicMock(name="terminal_manager")
    registry = MagicMock(name="terminal_runtime_registry")
    loop = MagicMock(name="loop")
    on_send_failure = MagicMock(name="on_send_failure")

    with patch(
        "gobby.sessions.clear_continuation.schedule_handoff_compact_continuation",
        return_value=True,
    ) as scheduled:
        assert schedule_handoff_continuation(
            session,
            "Continue.",
            loop=loop,
            db=db,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=registry,
            on_send_failure=on_send_failure,
        )
        assert schedule_handoff_continuation(session, "Continue.", delay_seconds=1.5)

    routed, delayed = scheduled.call_args_list
    assert routed.args == (session, "Continue.")
    assert routed.kwargs == {
        "loop": loop,
        "db": db,
        "terminal_manager": terminal_manager,
        "terminal_runtime_registry": registry,
        "on_send_failure": on_send_failure,
    }
    # The compaction scheduler owns the default send delay; it is passed only when set.
    assert delayed.kwargs == {
        "loop": None,
        "db": None,
        "terminal_manager": None,
        "terminal_runtime_registry": None,
        "on_send_failure": None,
        "delay_seconds": 1.5,
    }
