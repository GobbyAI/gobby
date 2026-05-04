"""Workflow hook tool-context rehydration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.git_utils import DirtyFiles
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

PLATFORM_SESSION_ID = "platform-claude-session"
EXTERNAL_SESSION_ID = "external-claude-session"

PROGRESSIVE_DISCOVERY_RULES = {
    "require-server-listed-for-schema",
    "require-schema-before-call",
    "track-schema-lookup",
    "track-servers-listed",
    "track-listed-servers",
    "reset-progressive-discovery",
}


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "test_tool_context_rehydration.db")
    run_migrations(database)
    return database


@pytest.fixture(autouse=True)
def clean_dirty_files(monkeypatch) -> None:
    monkeypatch.setattr(
        "gobby.workflows.git_utils.get_dirty_files_categorized",
        lambda *_args, **_kwargs: DirtyFiles(set(), set()),
    )


@pytest.fixture
def handler(db: LocalDatabase) -> WorkflowHookHandler:
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    db.execute("UPDATE workflow_definitions SET enabled = 0 WHERE workflow_type = 'rule'")
    for name in PROGRESSIVE_DISCOVERY_RULES:
        db.execute(
            "UPDATE workflow_definitions SET enabled = 1 WHERE name = ?",
            (name,),
        )

    return WorkflowHookHandler(rule_engine=RuleEngine(db))


def _event(
    event_type: HookEventType,
    data: dict[str, Any],
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=EXTERNAL_SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": PLATFORM_SESSION_ID},
    )


def _call_arguments(mcp_tool: str) -> dict[str, Any]:
    if mcp_tool == "create_task":
        return {
            "title": "test",
            "category": "code",
            "validation_criteria": "test task can be created",
        }
    if mcp_tool == "add_label":
        return {"task_id": "#1", "label": "test"}
    return {"task_id": "#1", "description": "updated"}


@pytest.mark.parametrize(
    ("mcp_tool", "schema_input"),
    [
        (
            "create_task",
            {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "session_id": "#3478",
            },
        ),
        ("add_label", {"server_name": "gobby-tasks", "tool_name": "add_label"}),
        ("update_task", {"server_name": "gobby-tasks", "tool_name": "update_task"}),
    ],
)
@pytest.mark.parametrize(
    "source",
    [SessionSource.CLAUDE, SessionSource.GEMINI, SessionSource.QWEN, SessionSource.DROID],
)
@pytest.mark.asyncio
async def test_cli_after_tool_rehydrates_schema_lookup_context(
    handler: WorkflowHookHandler,
    db: LocalDatabase,
    mcp_tool: str,
    schema_input: dict[str, Any],
    source: SessionSource,
) -> None:
    """CLI AFTER_TOOL events can omit tool_input; schema lookup still unlocks the tool."""
    session_vars = SessionVariableManager(db)
    session_vars.merge_variables(PLATFORM_SESSION_ID, {"listed_servers": ["gobby-tasks"]})

    before_event = _event(
        HookEventType.BEFORE_TOOL,
        {
            "tool_name": "mcp__gobby__get_tool_schema",
            "tool_input": schema_input,
            "mcp_server": "gobby",
            "mcp_tool": "get_tool_schema",
            "tool_use_id": f"schema-{mcp_tool}",
            "call_id": f"call-{mcp_tool}",
        },
        source=source,
    )
    before_response = await handler._evaluate_rules(before_event)
    assert before_response.decision == "allow"

    after_event = _event(
        HookEventType.AFTER_TOOL,
        {
            "tool_use_id": f"schema-{mcp_tool}",
            "tool_response": '{"success": true}',
        },
        source=source,
    )
    after_response = await handler._evaluate_rules(after_event)

    assert after_response.decision == "allow"
    assert after_event.data["tool_name"] == "mcp__gobby__get_tool_schema"
    assert after_event.data["tool_input"] == schema_input
    assert after_event.data["mcp_server"] == "gobby"
    assert after_event.data["mcp_tool"] == "get_tool_schema"
    assert after_event.data["call_id"] == f"call-{mcp_tool}"
    assert after_event.metadata["_tool_context_rehydrated"] is True
    assert after_event.metadata["_tool_context_rehydrated_source"] == source.value

    unlocked_tools = session_vars.get_variables(PLATFORM_SESSION_ID).get("unlocked_tools", [])
    assert f"gobby-tasks:{mcp_tool}" in unlocked_tools
    assert all(not key.startswith(f"gobby-tasks:{mcp_tool}:") for key in unlocked_tools)

    call_event = _event(
        HookEventType.BEFORE_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": mcp_tool,
                "arguments": _call_arguments(mcp_tool),
            },
        },
        source=source,
    )
    call_response = await handler._evaluate_rules(call_event)
    assert call_response.decision == "allow"
