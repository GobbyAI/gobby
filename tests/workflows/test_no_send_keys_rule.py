"""The default ``no-send-keys`` rule withholds ``send_keys`` unless a caller holds it.

``send_keys`` is granted to the operator's own unspawned terminal and to a
definition that declares the ``send_keys`` capability. Every other caller is
refused, including a caller whose session cannot be resolved. ``capture_output``
is not withheld by this rule. These tests run the real rule engine over the
synced bundled rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import AgentSelector
from gobby.workflows.engine.block_batching import operator_event_is_subagent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows

pytestmark = pytest.mark.unit

UNREGISTERED_CALLER_ID = "11111111-1111-4111-8111-111111111111"
CAPABLE_DEFINITION = "orchestrator-like"
SEAT_DEFINITION = "developer-like"


def _register(session_manager: SessionManager, project_id: str, name: str, depth: int) -> str:
    return session_manager.register(
        external_id=f"no-send-keys-{name}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
        agent_depth=depth,
    ).id


def _define_agent(db: HubDatabase, name: str, *, capabilities: tuple[str, ...] = ()) -> None:
    body = make_agent_definition(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name=name,
        capabilities=list(capabilities),
        workflows=make_agent_workflows(
            rules=[],
            rule_selectors=AgentSelector(include=["tag:default"], exclude=[]),
        ),
    )
    AgentDefinitionManager(db).create(name=name, definition_json=body.model_dump(), source="custom")


def _call_tool_event(caller_id: str, target_id: str, mcp_tool: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=caller_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": mcp_tool,
                "arguments": {"session_id": target_id},
            },
        },
        metadata={"_platform_session_id": caller_id},
    )


@pytest.fixture
def sessions(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> dict[str, str]:
    """Callers of each kind, plus an in-project target that is in scope for all of them."""
    project_id = sample_project["id"]
    _define_agent(temp_db, CAPABLE_DEFINITION, capabilities=("send_keys",))
    _define_agent(temp_db, SEAT_DEFINITION)
    callers = {
        "operator": _register(session_manager, project_id, "operator", depth=0),
        "capable": _register(session_manager, project_id, "capable", depth=1),
        "seat": _register(session_manager, project_id, "seat", depth=1),
        "unmanaged": _register(session_manager, project_id, "unmanaged", depth=1),
        "target": _register(session_manager, project_id, "target", depth=0),
    }
    variables = SessionVariableManager(temp_db)
    variables.set_variable(callers["capable"], "_agent_type", CAPABLE_DEFINITION)
    variables.set_variable(callers["seat"], "_agent_type", SEAT_DEFINITION)
    callers["unregistered"] = UNREGISTERED_CALLER_ID
    return callers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("operator", "allow"),
        ("capable", "allow"),
        ("seat", "block"),
        ("unmanaged", "block"),
        ("unregistered", "block"),
    ],
)
async def test_send_keys_needs_the_capability_or_the_operator_terminal(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sessions: dict[str, str],
    caller: str,
    expected: str,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    caller_id = sessions[caller]

    response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
        _call_tool_event(caller_id, sessions["target"], "send_keys"),
        session_id=caller_id,
        variables={},
    )

    assert response.decision == expected
    if expected == "block":
        assert "no-send-keys" in (response.reason or "")
        assert "gobby-agents:send_message" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["operator", "capable", "seat", "unmanaged", "unregistered"])
async def test_capture_output_is_not_withheld_by_this_rule(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sessions: dict[str, str],
    caller: str,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    caller_id = sessions[caller]

    response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
        _call_tool_event(caller_id, sessions["target"], "capture_output"),
        session_id=caller_id,
        variables={},
    )

    assert "no-send-keys" not in (response.reason or "")


def _bundled_definitions() -> dict[str, dict[str, Any]]:
    agents_dir = Path(get_bundled_rules_path()).parent / "agents"
    definitions: dict[str, dict[str, Any]] = {}
    for path in sorted(agents_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        definitions[data["name"]] = data
    return definitions


def test_only_orchestrator_and_assistant_carry_the_send_keys_capability() -> None:
    granted = {
        name
        for name, data in _bundled_definitions().items()
        if "send_keys" in (data.get("capabilities") or [])
    }

    assert granted == {"orchestrator", "assistant"}


def test_every_bundled_definition_selects_the_no_send_keys_rule() -> None:
    unselected = [
        name
        for name, data in _bundled_definitions().items()
        if not (
            {"tag:default", "name:no-send-keys"}
            & set(data["workflows"]["rule_selectors"]["include"])
        )
    ]

    assert unselected == []


@pytest.mark.parametrize(
    ("source", "event_data", "variables", "expected"),
    [
        # A spawned Gobby agent is not a subagent: the no-send-keys rule gates it.
        (SessionSource.CLAUDE, {}, {"is_spawned_agent": True}, False),
        (SessionSource.CODEX, {}, {"is_spawned_agent": True}, False),
        # A payload-named native subagent is one on any CLI.
        (SessionSource.CLAUDE, {"agent_id": "agent-uuid"}, {"is_spawned_agent": True}, True),
        (SessionSource.CLAUDE, {"agent_id": "agent-uuid"}, {}, True),
        # Claude omits the key on the parent thread even while is_subagent is set.
        (SessionSource.CLAUDE, {}, {"is_subagent": True}, False),
        # CLIs without per-event identity keep the session flag.
        (SessionSource.CODEX, {}, {"is_subagent": True}, True),
    ],
)
def test_operator_event_is_subagent_ignores_spawned_agents(
    source: SessionSource,
    event_data: dict[str, Any],
    variables: dict[str, Any],
    expected: bool,
) -> None:
    assert operator_event_is_subagent(event_data, source, variables) is expected
