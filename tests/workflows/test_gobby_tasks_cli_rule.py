"""Tests for the rules-engine guard around native task CLI mutations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "test_gobby_tasks_cli_rule.db")
    run_migrations(database)
    sync_bundled_rules(database, get_bundled_rules_path())
    database.execute(
        "UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'"
    )
    return database


@pytest.fixture
def effect(db: LocalDatabase) -> RuleEffect:
    manager = LocalWorkflowDefinitionManager(db)
    row = manager.get_by_name("block-gobby-tasks-cli")
    assert row is not None

    body = RuleDefinitionBody.model_validate_json(row.definition_json)
    assert body.event.value == "before_tool"
    assert body.resolved_effects[0].type == "block"
    return body.resolved_effects[0]


def _shell_event(tool_name: str, command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": tool_name, "tool_input": {"command": command}},
    )


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        "uv run gobby tasks create --help",
        "gobby tasks update #1 --priority 1",
        "gobby tasks close #1",
        "gobby tasks claim #1",
        "gobby tasks sync --export",
    ],
)
def test_blocks_mutating_task_cli_commands(
    db: LocalDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        "uv run gobby tasks list --ready",
        "gobby tasks ready",
        "gobby tasks blocked",
        "gobby tasks stats",
        "gobby tasks show #1",
        "gobby tasks expand validate-plan .gobby/plans/example.md",
        "gobby tasks validation_history #1",
        "gobby tasks doctor",
    ],
)
def test_allows_read_only_task_cli_commands(
    db: LocalDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is False
