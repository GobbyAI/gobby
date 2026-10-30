"""Project creation requires operator identity or a definition's explicit exemption."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.condition_helpers_sessions import session_condition_helpers
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("role", "state", "depth", "parent", "allowed"),
    [
        (None, "missing", 0, False, True),
        ("default", "missing", 0, True, True),
        (None, "missing", 1, False, False),
        ("default", "missing", 1, True, False),
        ("retired-seat", "missing", 0, False, False),
        ("retired-seat", "disabled", 0, True, False),
        ("custom-coordinator", "enabled", 0, True, True),
    ],
)
async def test_project_creation_identity_and_definition_exemption(
    temp_db: HubDatabase,
    role: str | None,
    state: str,
    depth: int,
    parent: bool,
    allowed: bool,
) -> None:
    bundle = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/workflows/rules/roles/spawned-session-policy.yaml"
    )
    document = yaml.safe_load(bundle.read_text())
    rules = RuleDefinitionManager(temp_db)
    for name, data in document["rules"].items():
        body, metadata = split_rule_definition_data(data)
        rules.create(
            name,
            RuleDefinitionBody.model_validate(body).model_dump(),
            tags=document["tags"],
            priority=metadata["priority"],
        )
    sessions = SessionManager(temp_db)
    parent_id = sessions.register_session(
        external_id="policy-parent",
        machine_id=None,
        source="claude",
        project_id=None,
    )
    session_id = sessions.register_session(
        external_id="policy-caller",
        machine_id=None,
        source="claude",
        project_id=None,
        parent_session_id=parent_id if parent else None,
        agent_depth=depth,
    )
    if role and state != "missing":
        AgentDefinitionManager(temp_db).create(
            role,
            {
                "name": role,
                "prompts": {"agent": "Fixture project creator."},
                "workflows": {
                    "rule_selectors": {
                        "include": ["tag:default"],
                        "exclude": ["name:non-operator-no-project-create"],
                    }
                },
            },
            enabled=state == "enabled",
        )
    SessionVariableManager(temp_db).set_variable(session_id, "_agent_type", role)
    response = await RuleEngine(temp_db).evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            source=SessionSource.CLAUDE,
            session_id=session_id,
            timestamp=datetime.now(UTC),
            metadata={"_platform_session_id": session_id},
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby",
                    "tool_name": "init_project",
                    "arguments": {},
                },
            },
        ),
        session_id=session_id,
        variables={"_agent_type": role, "is_spawned_agent": bool(depth)},
    )
    assert response.decision == ("allow" if allowed else "block")


@pytest.mark.parametrize("caller_id", [None, "", "#123", "00000000-0000-4000-8000-000000000123"])
def test_unresolved_identity_never_grants_operator_exemption(
    temp_db: HubDatabase,
    caller_id: str | None,
) -> None:
    helpers = session_condition_helpers(SessionManager(temp_db))
    assert helpers["operator_session"](caller_id) is False


def test_missing_session_manager_never_grants_operator_exemption() -> None:
    helpers = session_condition_helpers(None)
    assert helpers["operator_session"]("00000000-0000-4000-8000-000000000123") is False
