"""A resumed epoch consumes its durable handoff before ordinary tools."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import SessionContext, reset_session_context, set_session_context
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.workflows.test_block_tools_after_handoff_compact import HANDOFF_PREREQUISITE_EVENTS

pytestmark = pytest.mark.unit
SESSION_ID = "11111111-1111-4111-8111-111111111111"
RULE_NAME = "require-handoff-pull-before-tool"


@pytest.mark.parametrize("value", [None, True, {"attempt_id": "forged"}])
def test_pending_pull_marker_cannot_be_cleared_or_forged(value: Any) -> None:
    variables = MagicMock()
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = SESSION_ID
    token = set_session_context(SessionContext(session_id=SESSION_ID))
    try:
        with (
            patch(
                "gobby.mcp_proxy.tools.workflows._variables.resolve_session_id",
                return_value=SESSION_ID,
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager") as authority,
        ):
            authority.return_value.get_variable_subset.return_value = {"_agent_type": "default"}
            result = set_variable(
                sessions,
                MagicMock(),
                "handoff_pull_pending",
                value,
                SESSION_ID,
                session_var_manager=variables,
            )
    finally:
        reset_session_context(token)
    assert result["success"] is False
    assert "managed by the workflow runtime" in result["error"]
    variables.set_variable.assert_not_called()


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("UPDATE rule_definitions SET enabled = (name = %s)", (RULE_NAME,))
    return RuleEngine(temp_db)


def _event(data: dict[str, str]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "mcp__gobby__call_tool", **data},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_skill", "get_skill_file"])
async def test_pull_gate_blocks_skill_load_until_pull(engine: RuleEngine, tool: str) -> None:
    variables: dict[str, Any] = {"handoff_pull_pending": {"attempt_id": "a" * 32}}
    event = _event({"mcp_server": "gobby-skills", "mcp_tool": tool})
    blocked = await engine.evaluate(event, SESSION_ID, variables)
    assert blocked.decision == "block"
    assert RULE_NAME in (blocked.reason or "")

    variables.pop("handoff_pull_pending")
    allowed = await engine.evaluate(event, SESSION_ID, variables)
    assert allowed.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_event", HANDOFF_PREREQUISITE_EVENTS)
async def test_pull_gate_allows_handoff_prerequisite_tools(
    engine: RuleEngine, tool_event: dict[str, str]
) -> None:
    response = await engine.evaluate(
        _event(tool_event), SESSION_ID, {"handoff_pull_pending": {"attempt_id": "a" * 32}}
    )
    assert response.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["Bash", "Write", "exec_command"])
async def test_pull_gate_blocks_ordinary_tools(engine: RuleEngine, tool: str) -> None:
    response = await engine.evaluate(
        _event({"tool_name": tool}),
        SESSION_ID,
        {"handoff_pull_pending": {"attempt_id": "a" * 32}},
    )
    assert response.decision == "block"
    assert RULE_NAME in (response.reason or "")


@pytest.mark.parametrize(
    "rule_name",
    [
        "surface-memories-on-turn-start",
        "inject-planner-lessons",
        "inject-plan-reviewer-lessons",
        "inject-plan-enhancer-lessons",
        "inject-qa-reviewer-lessons",
    ],
)
def test_context_injection_waits_for_pull(temp_db: HubDatabase, rule_name: str) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    row = RuleDefinitionManager(temp_db).get_by_name(rule_name)
    assert row is not None
    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.when is not None
    variables: dict[str, Any] = {
        "parent_turn_seq": 1,
        "handoff_pull_pending": {"attempt_id": "a" * 32},
    }
    evaluator = SafeExpressionEvaluator(context={"variables": variables}, allowed_funcs={})
    assert evaluator.evaluate(body.when) is False
    variables.pop("handoff_pull_pending")
    assert evaluator.evaluate(body.when) is True


def test_retired_task_tree_helper_is_not_registered() -> None:
    assert "task_tree_complete" not in build_condition_helpers()


def test_retired_templates_do_not_reappear_on_sync(temp_db: HubDatabase) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    manager = RuleDefinitionManager(temp_db)
    assert manager.get_by_name("require-epic-tree-close") is None
    assert manager.list_by_group("auto-task") == []
