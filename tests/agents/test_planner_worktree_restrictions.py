"""Planning definitions deny worktree creation after spawn activation."""

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import seed_seat
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

PLANNERS = (
    "plan-writer",
    "plan-enhancer",
    "plan-adversary",
)


def _parse_row(payload: object) -> AgentDefinitionBody:
    if isinstance(payload, str):
        return AgentDefinitionBody.model_validate_json(payload)
    return AgentDefinitionBody.model_validate(payload)


@pytest.fixture
def seeded_agents(temp_db: HubDatabase) -> AgentDefinitionManager:
    """Synthetic planner seats carrying the worktree blocks, and an unrestricted developer."""
    for name in PLANNERS:
        seed_seat(
            temp_db,
            name,
            checkout_mode="none",
            blocked_tools=["EnterWorktree"],
            blocked_mcp_tools=["gobby-worktrees:create_worktree"],
        )
    seed_seat(temp_db, "developer")
    return AgentDefinitionManager(temp_db)


@pytest.mark.parametrize("name", (*PLANNERS, "developer"))
@pytest.mark.parametrize("tool", ("EnterWorktree", "create_worktree"))
def test_spawn_activated_session_worktree_enforcement(
    temp_db: HubDatabase,
    seeded_agents: AgentDefinitionManager,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    tool: str,
) -> None:
    row = seeded_agents.get_by_name(name)
    assert row is not None
    body = _parse_row(row.definition_json)
    assert body.supports_surface("spawn")
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: "21000000-0000-4000-8000-000000000001",
    )
    LocalMachineManager(temp_db).upsert_seen("21000000-0000-4000-8000-000000000001", TEST_USER_ID)
    session = SessionManager(temp_db).register(
        external_id=f"worktree-policy-{name}-{tool}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=None,
    )
    variables = SessionVariableManager(temp_db)
    changes, _, _ = build_definition_changes(body, session.id, temp_db, is_spawned=True)
    variables.merge_variables(session.id, changes)

    data: dict[str, Any] = {"tool_name": tool, "tool_input": {}}
    if tool == "create_worktree":
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-worktrees",
                "tool_name": tool,
                "arguments": {"branch_name": "forbidden-planner-worktree"},
            },
        }
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session.id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )
    response = RuleEngine(temp_db)._check_agent_tool_enforcement(
        event, session.id, variables.get_variables(session.id)
    )
    if name == "developer":
        assert response is None
    else:
        assert response is not None, (name, tool)
        assert response.decision == "block"
        assert tool in (response.reason or "")
