"""Capability rules use stored identity and enabled synthetic definitions."""

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("identity", "capability", "enabled", "depth", "target_tool", "allowed"),
    [
        ("fixture-admin", "fixture_permission", True, 1, "fixture_mutate", True),
        ("fixture-admin", "fixture_permission", True, 1, "fixture_create", True),
        ("fixture-limited", "fixture_limited_permission", True, 1, "fixture_mutate", True),
        ("fixture-limited", "fixture_limited_permission", True, 1, "fixture_create", False),
        ("fixture-reader", None, True, 1, "fixture_mutate", False),
        ("fixture-wrong", "other_permission", True, 1, "fixture_mutate", False),
        (None, None, None, 0, "fixture_mutate", True),
        (None, None, None, 1, "fixture_mutate", False),
        ("fixture-reader", None, True, 0, "fixture_mutate", False),
        ("fixture-reader", None, True, 1, "fixture_read", True),
    ],
)
@pytest.mark.parametrize("transport", ["wrapper", "native"])
@pytest.mark.parametrize("selection", ["included", "empty", "excluded"])
async def test_synthetic_capability_rule_uses_stored_authority(
    temp_db: HubDatabase,
    identity: str | None,
    capability: str | None,
    enabled: bool | None,
    depth: int,
    target_tool: str,
    allowed: bool,
    transport: str,
    selection: str,
) -> None:
    RuleDefinitionManager(temp_db).create(
        "fixture-capability-boundary",
        {
            "event": "before_tool",
            "mandatory": True,
            "when": (
                "event.data.get('mcp_server') == 'fixture-service' "
                "and event.data.get('mcp_tool') in ['fixture_mutate', 'fixture_create'] "
                "and not (operator_session(event.metadata.get('_platform_session_id')) "
                "or session_has_capability(event.metadata.get('_platform_session_id'), "
                "'fixture_permission') "
                "or (event.data.get('mcp_tool') == 'fixture_mutate' "
                "and session_has_capability(event.metadata.get('_platform_session_id'), "
                "'fixture_limited_permission')))"
            ),
            "effects": [
                {
                    "type": "block",
                    "mcp_tools": [
                        "fixture-service:fixture_mutate",
                        "fixture-service:fixture_create",
                    ],
                    "reason": "Requires fixture_permission.",
                }
            ],
        },
        tags=["default"],
    )
    if identity is not None and enabled is not None:
        AgentDefinitionManager(temp_db).create(
            identity,
            {
                "name": identity,
                "capabilities": [capability] if capability else [],
                "prompts": {"agent": "Synthetic configuration caller."},
                "workflows": {
                    "rule_selectors": {
                        "include": [] if selection == "empty" else ["tag:default"],
                        "exclude": ["name:fixture-capability-boundary"]
                        if selection == "excluded"
                        else [],
                    }
                },
            },
            enabled=enabled,
        )
    session_id = SessionManager(temp_db).register_session(
        external_id="fixture-capability-caller",
        machine_id=None,
        source="claude",
        project_id=None,
        agent_depth=depth,
    )
    SessionVariableManager(temp_db).set_variable(session_id, "_agent_type", identity)
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
                    "server_name": "fixture-service",
                    "tool_name": target_tool,
                    "arguments": {},
                },
            }
            if transport == "wrapper"
            else {
                "tool_name": f"mcp__fixture-service__{target_tool}",
                "tool_input": {},
            },
        ),
        session_id=session_id,
        # Claimed permissions in mutable input cannot grant authority.
        variables={"_agent_type": identity, "capabilities": ["fixture_permission"]},
    )
    assert response.decision == ("allow" if allowed else "block")
    if not allowed:
        assert "fixture_permission" in (response.reason or "")
