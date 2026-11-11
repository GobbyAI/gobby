"""A seat sets and clears its own status line; no other session's line is reachable."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._status_line import register_status_line_tools
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from tests.storage.tasks.test_sweep_stale_claims import _make_session

pytestmark = pytest.mark.unit

STATUS_LINE_CAP = 120


class _TestRegistry(InternalToolRegistry):
    def get_tool(self, name: str) -> Callable[..., Any]:
        return self._tools[name].func


def _registry(temp_db: HubDatabase) -> _TestRegistry:
    registry = _TestRegistry(name="gobby-sessions", description="test")
    register_status_line_tools(registry, SessionManager(temp_db))
    return registry


def _seat(temp_db: HubDatabase, project: dict[str, Any]) -> str:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, project, session_id, "active")
    return session_id


def _stored(temp_db: HubDatabase, session_id: str) -> str | None:
    row = temp_db.fetchone("SELECT status_line FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    status_line: str | None = row["status_line"]
    return status_line


def test_seat_sets_and_clears_its_own_status_line(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seat = _seat(temp_db, sample_project)
    set_tool = _registry(temp_db).get_tool("set_status_line")

    with session_context_for_test(seat):
        set_result = asyncio.run(set_tool(status_line="Reviewing #25480"))
        stored = _stored(temp_db, seat)
        cleared = asyncio.run(set_tool(status_line=None))

    assert (set_result["success"], set_result["session_id"]) == (True, seat)
    assert stored == "Reviewing #25480"
    assert (cleared["success"], _stored(temp_db, seat)) == (True, None)


def test_naming_the_callers_own_session_is_allowed(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seat = _seat(temp_db, sample_project)
    set_tool = _registry(temp_db).get_tool("set_status_line")

    with session_context_for_test(seat):
        result = asyncio.run(set_tool(status_line="Waiting on review", session_id=seat))

    assert (result["success"], _stored(temp_db, seat)) == (True, "Waiting on review")


def test_a_seat_cannot_set_another_sessions_line(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seat = _seat(temp_db, sample_project)
    other = _seat(temp_db, sample_project)
    set_tool = _registry(temp_db).get_tool("set_status_line")

    with session_context_for_test(seat):
        refused = asyncio.run(set_tool(status_line="Taking over", session_id=other))

    assert (refused["success"], refused["error_code"]) == (
        False,
        "status_line_target_forbidden",
    )
    assert _stored(temp_db, other) is None


def test_a_call_without_session_context_is_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seat = _seat(temp_db, sample_project)
    set_tool = _registry(temp_db).get_tool("set_status_line")

    refused = asyncio.run(set_tool(status_line="Working"))

    assert (refused["success"], refused["error_code"]) == (False, "status_line_caller_unknown")
    assert _stored(temp_db, seat) is None


def test_a_line_past_the_cap_is_refused_and_the_last_line_stays(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    seat = _seat(temp_db, sample_project)
    set_tool = _registry(temp_db).get_tool("set_status_line")

    with session_context_for_test(seat):
        asyncio.run(set_tool(status_line="Kept"))
        refused = asyncio.run(set_tool(status_line="x" * (STATUS_LINE_CAP + 1)))

    assert (refused["success"], refused["error_code"]) == (False, "status_line_too_long")
    assert _stored(temp_db, seat) == "Kept"
