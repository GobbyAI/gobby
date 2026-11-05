"""send_wake's caller gate admits the operator terminal and granted capabilities only."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

BUNDLED_RULES = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/rules"
WAKE_TOOL = "gobby-sessions:send_wake"


@pytest.mark.parametrize(
    ("identity", "capabilities", "depth", "target_tool", "allowed"),
    [
        (None, [], 0, "send_wake", True),
        (None, [], 1, "send_wake", False),
        ("fixture-wake", ["send_wake"], 1, "send_wake", True),
        ("fixture-wake", ["send_keys"], 1, "send_wake", False),
        ("fixture-plain", [], 1, "send_wake", False),
        ("fixture-plain", [], 1, "send_keys", True),
    ],
)
async def test_send_wake_gate_uses_stored_capabilities(
    temp_db: HubDatabase,
    identity: str | None,
    capabilities: list[str],
    depth: int,
    target_tool: str,
    allowed: bool,
) -> None:
    RuleDefinitionManager(temp_db).create(
        "fixture-send-wake-gate",
        {
            "event": "before_tool",
            "mandatory": True,
            "when": (
                "tool_input.get('server_name') == 'gobby-sessions' "
                "and tool_input.get('tool_name') == 'send_wake' "
                "and not (operator_session(event.metadata.get('_platform_session_id')) "
                "or session_has_capability(event.metadata.get('_platform_session_id'), "
                "'send_wake'))"
            ),
            "effects": [
                {
                    "type": "block",
                    "mcp_tools": [WAKE_TOOL],
                    "reason": "Requires the send_wake capability.",
                }
            ],
        },
        tags=["default"],
    )
    if identity is not None:
        AgentDefinitionManager(temp_db).create(
            identity,
            {
                "name": identity,
                "capabilities": capabilities,
                "prompts": {"agent": "Synthetic wake caller."},
                "workflows": {"rule_selectors": {"include": ["tag:default"]}},
            },
            enabled=True,
        )
    session_id = SessionManager(temp_db).register_session(
        external_id="fixture-wake-caller",
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
                    "server_name": "gobby-sessions",
                    "tool_name": target_tool,
                    "arguments": {"session_id": "fixture-target"},
                },
            },
        ),
        session_id=session_id,
        # Claimed capabilities in mutable input cannot grant the wake.
        variables={"_agent_type": identity, "capabilities": ["send_wake"]},
    )
    assert response.decision == ("allow" if allowed else "block")
    if not allowed:
        assert "send_wake" in (response.reason or "")


def test_bundled_rules_gate_send_wake_with_versioned_block_effects() -> None:
    gating_files: set[Path] = set()
    for path in sorted(BUNDLED_RULES.rglob("*.yaml")):
        if "deprecated" in path.parts:
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for rule in (document.get("rules") or {}).values():
            for effect in rule.get("effects") or []:
                if WAKE_TOOL not in effect.get("mcp_tools", []):
                    continue
                assert effect["type"] == "block"
                assert "when" in rule
                assert isinstance(document.get("version"), str)
                gating_files.add(path)
    assert gating_files
