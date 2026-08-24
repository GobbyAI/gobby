from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.config.features import ToolResultOffloadConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.services.result_offload import ToolResultOffloader
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tool_results import ToolResultStore
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.integration

RAW_TOOL_SESSION_ID = "22222222-2222-4222-8222-222222222222"
PLATFORM_SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    database = temp_db
    sync_bundled_rules(database, get_bundled_rules_path())
    database.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
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
        session_id=RAW_TOOL_SESSION_ID,
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
    # session_variables.session_id is a native uuid column
    platform_session_id = PLATFORM_SESSION_ID
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


@pytest.mark.asyncio
async def test_oversized_get_skill_wrapper_result_survives_codex_normalization_and_compaction(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    platform_session_id = PLATFORM_SESSION_ID
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
    oversized_content = "instructions-" + ("x" * 16_000)
    oversized_skill = {
        "success": True,
        "skill": {"name": "tasks", "content": oversized_content},
    }

    def get_oversized_skill(name: str) -> dict[str, object]:
        del name
        return oversized_skill

    registry = InternalToolRegistry("gobby-skills")
    registry.register(
        name="get_skill",
        description="Return an oversized skill.",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        func=get_oversized_skill,
    )
    manager = InternalRegistryManager()
    manager.add_registry(registry)
    config = ToolResultOffloadConfig(
        threshold_chars=3_000,
        max_envelope_chars=2_000,
        preview_chars=200,
        chunk_chars=200,
        max_stored_chars=20_000,
        exempt_tools=[],
    )
    mcp_manager = MagicMock()
    mcp_manager.project_id = None
    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=manager,
        validate_arguments=False,
        result_offloader=ToolResultOffloader(
            ToolResultStore(db, config),
            db,
            config,
            lambda: None,
        ),
    )

    async def load_and_observe() -> None:
        full_result = await proxy.call_tool(
            "gobby-skills",
            "get_skill",
            {"name": "tasks"},
            wrapper_originated=True,
        )
        assert full_result == {"skill": oversized_skill["skill"]}
        assert full_result["skill"]["content"] == oversized_content
        assert "offloaded" not in full_result
        event = _event(
            HookEventType.AFTER_TOOL,
            cwd=tmp_path,
            platform_session_id=platform_session_id,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-skills",
                    "tool_name": "get_skill",
                    "arguments": {"name": "tasks"},
                },
                "tool_output": {"success": True, "result": full_result},
            },
        )
        response = await handler.evaluate_async(event)
        assert response.decision == "allow"
        assert session_vars.get_variables(platform_session_id)["loaded_skills"] == ["tasks"]

    await load_and_observe()

    compact_response = await handler.evaluate_async(
        _event(
            HookEventType.SESSION_START,
            cwd=tmp_path,
            platform_session_id=platform_session_id,
            data={"source": "compact"},
        )
    )
    assert compact_response.decision == "allow"
    assert session_vars.get_variables(platform_session_id)["loaded_skills"] == []

    lifecycle_response = await handler.evaluate_async(
        _event(
            HookEventType.BEFORE_TOOL,
            cwd=tmp_path,
            platform_session_id=platform_session_id,
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                },
            },
        )
    )
    assert lifecycle_response.decision == "block"
    assert skill_fetch_directive("tasks") in (lifecycle_response.reason or "")

    await load_and_observe()
