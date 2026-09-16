"""Integration tests for is_subagent variable and rule engine interaction.

Verifies that:
- block-native-task-tracker-unclaimed fires when is_subagent is False and task_claimed is False
- block-native-task-tracker-unclaimed is skipped when is_subagent is True or task_claimed is True
- reset-subagent-flag clears is_subagent on turn_start
- Bidirectional toggle works within same session
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.integration

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"
EXTERNAL_SESSION_ID = "22222222-2222-4222-8222-222222222222"
NATIVE_TRACKER_TOOLS = (
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TodoWrite",
    "todo_write",
    "update_plan",
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def engine(db: HubDatabase) -> RuleEngine:
    """Sync bundled rules and enable only the ones we're testing."""
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    # Disable everything, then enable only our target rules
    db.execute("UPDATE rule_definitions SET enabled = FALSE")
    for name in (
        "block-native-task-tracker-unclaimed",
        "reset-subagent-flag",
    ):
        db.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
            (name,),
        )
    return RuleEngine(db)


def _make_hook_event(
    event_type: HookEventType,
    tool_name: str = "",
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": tool_name},
        metadata={"_platform_session_id": SESSION_ID},
    )


class TestSubagentRuleIntegration:
    """End-to-end: RuleEngine.evaluate() with is_subagent variable."""

    @pytest.mark.asyncio
    async def test_blocks_task_tools_when_unclaimed(self, engine) -> None:
        """TaskCreate should be blocked when not subagent and no task claimed."""
        variables: dict = {"is_subagent": False}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TaskCreate")
        result = await engine.evaluate(event, SESSION_ID, variables)

        assert result.decision == "block"
        assert "gobby-task" in (result.reason or "").lower()

    @pytest.mark.asyncio
    async def test_blocks_task_tools_when_variables_unset(self, engine) -> None:
        """TaskCreate should be blocked when neither is_subagent nor task_claimed is set."""
        variables: dict = {}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TaskCreate")
        result = await engine.evaluate(event, SESSION_ID, variables)

        assert result.decision == "block"

    @pytest.mark.asyncio
    async def test_allows_task_tools_when_subagent(self, engine) -> None:
        """TaskCreate should be allowed when is_subagent is True."""
        variables: dict = {"is_subagent": True}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TaskCreate")
        result = await engine.evaluate(event, SESSION_ID, variables)

        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_task_tools_when_task_claimed(self, engine) -> None:
        """Every native tracker tool should be allowed when a Gobby task is claimed."""
        variables: dict = {"is_subagent": False, "task_claimed": True}
        for tool in NATIVE_TRACKER_TOOLS:
            event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name=tool)
            result = await engine.evaluate(event, SESSION_ID, variables)
            assert result.decision == "allow", f"{tool} should be allowed with task claimed"

    @pytest.mark.asyncio
    async def test_allows_all_tools_when_subagent(self, engine) -> None:
        """Every native tracker tool should be allowed for subagents."""
        variables: dict = {"is_subagent": True}
        for tool in NATIVE_TRACKER_TOOLS:
            event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name=tool)
            result = await engine.evaluate(event, SESSION_ID, variables)
            assert result.decision == "allow", f"{tool} should be allowed for subagent"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", NATIVE_TRACKER_TOOLS)
    async def test_claim_lifecycle_gates_tracker(self, engine, tool: str) -> None:
        """Blocked before a claim, allowed while claimed, blocked again after close."""
        variables: dict = {"is_subagent": False, "task_claimed": False}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name=tool)

        result = await engine.evaluate(event, SESSION_ID, variables)
        assert result.decision == "block"

        variables["task_claimed"] = True
        result = await engine.evaluate(event, SESSION_ID, variables)
        assert result.decision == "allow"

        # close_task releases the last claim, which sets task_claimed back to False.
        variables["task_claimed"] = False
        result = await engine.evaluate(event, SESSION_ID, variables)
        assert result.decision == "block"

    @pytest.mark.asyncio
    async def test_reset_rule_clears_is_subagent_on_turn_start(self, engine) -> None:
        """reset-subagent-flag should set is_subagent=False on turn_start."""
        variables: dict = {"is_subagent": True}
        event = _make_hook_event(HookEventType.BEFORE_AGENT)
        result = await engine.evaluate(event, SESSION_ID, variables)

        assert result.decision == "allow"
        # The set_variable effect should have mutated variables in-place
        assert variables["is_subagent"] is False

    @pytest.mark.asyncio
    async def test_reset_rule_noop_when_already_false(self, engine) -> None:
        """reset-subagent-flag should not fire when is_subagent is already False."""
        variables: dict = {"is_subagent": False}
        event = _make_hook_event(HookEventType.BEFORE_AGENT)
        result = await engine.evaluate(event, SESSION_ID, variables)

        assert result.decision == "allow"
        assert variables["is_subagent"] is False
