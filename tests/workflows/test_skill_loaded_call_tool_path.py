from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.integration


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    database = temp_db
    sync_bundled_rules(database, get_bundled_rules_path())
    database.execute(
        "UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'"
    )
    yield database


def _event(
    event_type: HookEventType,
    *,
    data: dict[str, Any],
    cwd: Path,
    platform_session_id: str,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="external-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": platform_session_id},
        cwd=str(cwd),
    )


@pytest.mark.asyncio
async def test_raw_get_skill_after_tool_populates_loaded_skill_for_call_tool_path(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    platform_session_id = "skill-loaded-call-tool-session"
    session_vars = SessionVariableManager(db)
    session_vars.merge_variables(
        platform_session_id,
        {
            "baseline_dirty_files": [],
            "session_edited_files": [],
            "enforce_tool_schema_check": False,
        },
    )
    handler = WorkflowHookHandler(rule_engine=RuleEngine(db))

    raw_get_skill_after_tool = _event(
        HookEventType.AFTER_TOOL,
        cwd=tmp_path,
        platform_session_id=platform_session_id,
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-skills",
                "tool_name": "get_skill",
                "arguments": {"name": "build-coordinator"},
            },
            "tool_output": {
                "success": True,
                "result": {"skill": {"name": "build-coordinator"}},
            },
        },
    )

    load_response = await handler.evaluate_async(raw_get_skill_after_tool)

    assert load_response.decision == "allow"
    assert raw_get_skill_after_tool.data["mcp_server"] == "gobby-skills"
    assert raw_get_skill_after_tool.data["mcp_tool"] == "get_skill"

    schema_event = _event(
        HookEventType.BEFORE_TOOL,
        cwd=tmp_path,
        platform_session_id=platform_session_id,
        data={
            "tool_name": "mcp__gobby__get_tool_schema",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "get_session",
            },
        },
    )
    call_event = _event(
        HookEventType.BEFORE_TOOL,
        cwd=tmp_path,
        platform_session_id=platform_session_id,
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "get_session",
                "arguments": {"session_id": "target-session"},
            },
        },
    )

    schema_response = await handler.evaluate_async(schema_event)
    call_response = await handler.evaluate_async(call_event)

    assert schema_response.decision == "allow"
    assert call_response.decision == "allow"

    variables = session_vars.get_variables(platform_session_id)
    assert variables["loaded_skills"] == ["build-coordinator"]
