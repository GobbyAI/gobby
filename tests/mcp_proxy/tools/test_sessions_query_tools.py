"""list_sessions and get_session must agree on per-session task-ref arrays."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._crud import register_crud_tools
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit

_REFS_CLAIMED: dict[str, list[int]] = {
    "claimed": [22322],
    "created": [22000],
    "closed": [21000],
}
_REFS_IDLE: dict[str, list[int]] = {"claimed": [], "created": [], "closed": []}


def _make_session(**overrides: Any) -> Session:
    defaults: dict[str, Any] = {
        "id": "sess-claimed",
        "external_id": "ext-claimed",
        "machine_id": "21000000-0000-4000-8000-000000000001",
        "source": "grok",
        "project_id": "proj-xyz",
        "title": "Claimed session",
        "status": "active",
        "transcript_path": None,
        "summary_path": None,
        "summary_markdown": None,
        "git_branch": "task-22322",
        "parent_session_id": None,
        "created_at": "2026-09-14T00:00:00+00:00",
        "updated_at": "2026-09-14T00:00:00+00:00",
        "seq_num": 13227,
    }
    defaults.update(overrides)
    return Session(**defaults)


def _registry(session_manager: Any) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="gobby-sessions", description="task-ref projection tests")
    register_crud_tools(registry, session_manager)
    return registry


def _fetch_side_effect(session_ids: list[str]) -> dict[str, dict[str, list[int]]]:
    known = {
        "sess-claimed": _REFS_CLAIMED,
        "sess-idle": _REFS_IDLE,
    }
    return {sid: known.get(sid, _REFS_IDLE) for sid in session_ids}


def _session_manager(*sessions: Session) -> MagicMock:
    manager = MagicMock()
    manager.list.return_value = list(sessions)
    manager.count.return_value = len(sessions)
    manager.fetch_task_refs_by_session.side_effect = _fetch_side_effect
    return manager


def test_list_sessions_and_get_session_agree_on_task_refs() -> None:
    """Listing must not serialize default [] while get_session has the claims."""
    listed_claimed = _make_session(id="sess-claimed")
    listed_idle = _make_session(id="sess-idle", external_id="ext-idle", seq_num=1)
    single_claimed = _make_session(id="sess-claimed")
    manager = _session_manager(listed_claimed, listed_idle)
    manager.resolve_session_reference.return_value = single_claimed.id
    manager.get.return_value = single_claimed
    registry = _registry(manager)
    list_sessions = registry.get_tool("list_sessions")
    get_session = registry.get_tool("get_session")
    assert list_sessions is not None
    assert get_session is not None

    listed = list_sessions()
    manager.fetch_task_refs_by_session.assert_called_once_with(["sess-claimed", "sess-idle"])

    listed_by_id = {row["id"]: row for row in listed["sessions"]}
    assert listed_by_id["sess-claimed"]["claimed_task_refs"] == [22322]
    assert listed_by_id["sess-claimed"]["created_task_refs"] == [22000]
    assert listed_by_id["sess-claimed"]["closed_task_refs"] == [21000]
    assert listed_by_id["sess-idle"]["claimed_task_refs"] == []
    assert listed_by_id["sess-idle"]["created_task_refs"] == []
    assert listed_by_id["sess-idle"]["closed_task_refs"] == []

    with patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"id": "proj-xyz"},
    ):
        single = get_session(session_id="sess-claimed")

    assert single["claimed_task_refs"] == listed_by_id["sess-claimed"]["claimed_task_refs"]
    assert single["created_task_refs"] == listed_by_id["sess-claimed"]["created_task_refs"]
    assert single["closed_task_refs"] == listed_by_id["sess-claimed"]["closed_task_refs"]
    assert manager.fetch_task_refs_by_session.call_args_list[1].args[0] == ["sess-claimed"]


def test_list_sessions_warning_path_still_populates_task_refs() -> None:
    claimed = _make_session()
    manager = _session_manager(claimed)
    list_sessions = _registry(manager).get_tool("list_sessions")
    assert list_sessions is not None

    result = list_sessions(status="active", limit=1)

    assert "warning" in result
    assert result["sessions"][0]["claimed_task_refs"] == [22322]
    assert result["sessions"][0]["created_task_refs"] == [22000]
    assert result["sessions"][0]["closed_task_refs"] == [21000]
    manager.fetch_task_refs_by_session.assert_called_once_with(["sess-claimed"])
