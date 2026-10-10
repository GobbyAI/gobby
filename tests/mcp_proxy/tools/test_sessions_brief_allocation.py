"""Brief session lists must not allocate stored conversation bodies."""

from __future__ import annotations

import gc
import tracemalloc
from typing import Any

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._crud import register_crud_tools
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.integration

MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("status,limit", [(None, 20), ("active", 1)])
def test_brief_list_allocation_ignores_conversation_bodies(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, status: str | None, limit: int
) -> None:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    manager = SessionManager(temp_db)
    sessions = [
        manager.register(
            external_id=f"brief-allocation-{index}",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=None,
            terminal_context={"tmux_pane": f"%{index}"},
            workspace_path=f"/isolated/workspace-{index}",
            sandbox_enabled=True,
        )
        for index in range(2)
    ]
    manager.register(
        external_id="brief-other-source", machine_id=MACHINE_ID, source="grok", project_id=None
    )
    registry = InternalToolRegistry(name="gobby-sessions", description="allocation regression")
    register_crud_tools(registry, manager)
    list_sessions = registry.get_tool("list_sessions")
    assert list_sessions is not None
    filters: dict[str, Any] = {
        "project_id": sessions[0].project_id,
        "source": "codex",
        "machine_id": MACHINE_ID,
        "status": status,
        "limit": limit,
    }

    peaks: list[int] = []
    for size in (1024, 4 * 1024 * 1024):
        body = "x" * size
        for session in sessions:
            temp_db.execute(
                """UPDATE sessions SET summary_markdown = %s, handoff_markdown = %s,
                   original_prompt = %s, last_assistant_content = %s WHERE id = %s""",
                (body, body, body, body, session.id),
            )
        full = manager.list(**filters)
        manager.load_task_refs(full)
        expected = [session.to_brief() for session in full]
        gc.collect()
        tracemalloc.start()
        try:
            result = list_sessions(**filters)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
        assert result["sessions"] == expected
        assert result["total"] == 2
        assert result["count"] == min(limit, 2)
        assert result["filters"] == {
            **{key: value for key, value in filters.items() if key != "limit"},
            "agent_type": None,
        }
        assert ("warning" in result) is (status == "active" and limit == 1)
        assert all(session["can_proxy_attach"] for session in result["sessions"])
        assert all(session["claimed_task_refs"] == [] for session in result["sessions"])
        # Full storage reads must retain their existing body contract.
        assert all(session.summary_markdown == body for session in full)
        assert all(session.handoff_markdown == body for session in full)
        assert all(session.original_prompt == body for session in full)
        assert all(session.last_assistant_content == body for session in full)

    print(f"brief list allocation: status={status} small={peaks[0]} large={peaks[1]}")
    assert peaks[1] < peaks[0] + 512 * 1024


@pytest.mark.parametrize(
    "status,session_type,terminal_context,expected_attach",
    [
        ("active", "terminal", {"tmux_pane": "%1"}, True),
        ("paused", "terminal", {"tmux_pane": "%1"}, True),
        ("expired", "terminal", {"tmux_pane": "%1"}, False),
        ("active", "terminal", None, False),
        ("active", "terminal", {"tmux_pane": ""}, False),
        ("active", "web_chat", {"tmux_pane": "%1"}, False),
    ],
)
def test_brief_list_preserves_attach_liveness(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    session_type: str,
    terminal_context: dict[str, str] | None,
    expected_attach: bool,
) -> None:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    manager = SessionManager(temp_db)
    session = manager.register(
        external_id="brief-liveness",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=None,
        session_type=session_type,
        terminal_context=terminal_context,
    )
    manager.update_status(session.id, status)
    registry = InternalToolRegistry(name="gobby-sessions", description="liveness regression")
    register_crud_tools(registry, manager)
    list_sessions = registry.get_tool("list_sessions")
    assert list_sessions is not None

    result = list_sessions(source="codex", machine_id=MACHINE_ID, status=status)

    assert result["count"] == 1
    assert result["sessions"][0]["id"] == session.id
    assert result["sessions"][0]["can_proxy_attach"] is expected_attach


def test_list_sessions_filters_by_agent_type(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    manager = SessionManager(temp_db)
    variables = SessionVariableManager(temp_db)
    typed = {
        "lane-manager": manager.register(
            external_id="agent-type-a", machine_id=MACHINE_ID, source="codex", project_id=None
        ),
        "reporter": manager.register(
            external_id="agent-type-b", machine_id=MACHINE_ID, source="codex", project_id=None
        ),
    }
    manager.register(
        external_id="agent-type-untyped", machine_id=MACHINE_ID, source="codex", project_id=None
    )
    for agent_type, session in typed.items():
        variables.set_variable(session.id, "_agent_type", agent_type)
    registry = InternalToolRegistry(name="gobby-sessions", description="agent type filter")
    register_crud_tools(registry, manager)
    list_sessions = registry.get_tool("list_sessions")
    assert list_sessions is not None

    result = list_sessions(machine_id=MACHINE_ID, agent_type="lane-manager")

    assert [row["id"] for row in result["sessions"]] == [typed["lane-manager"].id]
    assert result["total"] == 1
    assert result["filters"]["agent_type"] == "lane-manager"
    assert list_sessions(machine_id=MACHINE_ID, agent_type="nobody")["total"] == 0
    assert list_sessions(machine_id=MACHINE_ID)["total"] == 3
