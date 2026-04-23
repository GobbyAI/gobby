"""Integration tests for is_subagent variable and rule engine interaction.

Verifies that:
- block-native-task-tools-unclaimed fires when is_subagent is False and task_claimed is False
- block-native-task-tools-unclaimed is skipped when is_subagent is True or task_claimed is True
- block-native-todo-write fires when is_subagent is False (regardless of task_claimed)
- reset-subagent-flag clears is_subagent on turn_start
- Bidirectional toggle works within same session
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path: Path) -> LocalDatabase:
    db_path = tmp_path / "test_subagent_rules.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def engine(db: LocalDatabase) -> RuleEngine:
    """Sync bundled rules and enable only the ones we're testing."""
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    # Disable everything, then enable only our target rules
    db.execute("UPDATE workflow_definitions SET enabled = 0")
    for name in (
        "block-native-task-tools-unclaimed",
        "block-native-todo-write",
        "reset-subagent-flag",
    ):
        db.execute(
            "UPDATE workflow_definitions SET enabled = 1 WHERE name = ?",
            (name,),
        )
    return RuleEngine(db)


def _make_hook_event(
    event_type: HookEventType,
    tool_name: str = "",
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session-ext",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": tool_name},
        metadata={"_platform_session_id": "test-session"},
    )


class TestSubagentRuleIntegration:
    """End-to-end: RuleEngine.evaluate() with is_subagent variable."""

    @pytest.mark.asyncio
    async def test_blocks_task_tools_when_unclaimed(self, engine) -> None:
        """TaskCreate should be blocked when not subagent and no task claimed."""
        variables: dict = {"is_subagent": False}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TaskCreate")
        result = await engine.evaluate(event, "test-session", variables)

        assert result.decision == "block"
        assert "gobby task" in (result.reason or "").lower()

    @pytest.mark.asyncio
    async def test_blocks_task_tools_when_variables_unset(self, engine) -> None:
        """TaskCreate should be blocked when neither is_subagent nor task_claimed is set."""
        variables: dict = {}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TaskCreate")
        result = await engine.evaluate(event, "test-session", variables)

        assert result.decision == "block"

    @pytest.mark.asyncio
    async def test_allows_task_tools_when_subagent(self, engine) -> None:
        """TaskCreate should be allowed when is_subagent is True."""
        variables: dict = {"is_subagent": True}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TaskCreate")
        result = await engine.evaluate(event, "test-session", variables)

        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_task_tools_when_task_claimed(self, engine) -> None:
        """TaskCreate should be allowed when a Gobby task is claimed."""
        variables: dict = {"is_subagent": False, "task_claimed": True}
        for tool in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList"):
            event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name=tool)
            result = await engine.evaluate(event, "test-session", variables)
            assert result.decision == "allow", f"{tool} should be allowed with task claimed"

    @pytest.mark.asyncio
    async def test_todo_write_blocked_even_with_task_claimed(self, engine) -> None:
        """TodoWrite should be blocked regardless of task_claimed."""
        variables: dict = {"is_subagent": False, "task_claimed": True}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TodoWrite")
        result = await engine.evaluate(event, "test-session", variables)

        assert result.decision == "block"

    @pytest.mark.asyncio
    async def test_allows_all_tools_when_subagent(self, engine) -> None:
        """All native task tools including TodoWrite should be allowed for subagents."""
        variables: dict = {"is_subagent": True}
        for tool in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TodoWrite"):
            event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name=tool)
            result = await engine.evaluate(event, "test-session", variables)
            assert result.decision == "allow", f"{tool} should be allowed for subagent"

    @pytest.mark.asyncio
    async def test_bidirectional_toggle(self, engine) -> None:
        """Toggling task_claimed should change blocking behavior for task tools."""
        variables: dict = {"is_subagent": False, "task_claimed": False}
        event = _make_hook_event(HookEventType.BEFORE_TOOL, tool_name="TaskCreate")

        # Blocked when unclaimed
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "block"

        # Allowed when claimed
        variables["task_claimed"] = True
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

        # Blocked again when unclaimed
        variables["task_claimed"] = False
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "block"

    @pytest.mark.asyncio
    async def test_reset_rule_clears_is_subagent_on_turn_start(self, engine) -> None:
        """reset-subagent-flag should set is_subagent=False on turn_start."""
        variables: dict = {"is_subagent": True}
        event = _make_hook_event(HookEventType.BEFORE_AGENT)
        result = await engine.evaluate(event, "test-session", variables)

        assert result.decision == "allow"
        # The set_variable effect should have mutated variables in-place
        assert variables["is_subagent"] is False

    @pytest.mark.asyncio
    async def test_reset_rule_noop_when_already_false(self, engine) -> None:
        """reset-subagent-flag should not fire when is_subagent is already False."""
        variables: dict = {"is_subagent": False}
        event = _make_hook_event(HookEventType.BEFORE_AGENT)
        result = await engine.evaluate(event, "test-session", variables)

        assert result.decision == "allow"
        assert variables["is_subagent"] is False
