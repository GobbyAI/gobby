"""The status-report tick wakes the one live Assistant session and refuses otherwise.

The session listing runs the real gobby-sessions list_sessions tool over temp_db, so the
per-status filter and the listing limit apply as they do in production.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._crud import register_crud_tools
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.integration

PROJECT_ID = "00000000-0000-0000-0000-000000000000"
PIPELINE = "assistant-status-wake"
LIVE_STATUSES = (
    "active",
    "paused",
    "waiting",
    "idle",
    "interrupted",
    "awaiting_input",
    "awaiting_approval",
    "awaiting_handoff",
)
LISTING_LIMIT = 500
HISTORICAL_ROWS = LISTING_LIMIT + 1


class StatusTickProxy:
    """Stub gobby-sessions and gobby-agents proxy: list_sessions is real, sends are recorded."""

    def __init__(self, temp_db: HubDatabase) -> None:
        registry = InternalToolRegistry(name="gobby-sessions", description="status tick tests")
        register_crud_tools(registry, SessionManager(temp_db))
        list_sessions = registry.get_tool("list_sessions")
        assert list_sessions is not None
        self.list_sessions: Callable[..., dict[str, Any]] = list_sessions
        self.calls: list[dict[str, Any]] = []

    async def get_tool_schema(
        self, server: str, tool: str, *, session_id: str | None
    ) -> dict[str, Any]:
        return {"success": True, "tool": {"inputSchema": {}}}

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        self.calls.append({"tool": f"{server}:{tool}", "arguments": arguments})
        if tool == "list_sessions":
            return self.list_sessions(**arguments)
        if tool == "send_message":
            return {"success": True, "delivery_status": "sent", "message_ids": ["m-1"]}
        return {"success": True}

    def listings(self) -> list[dict[str, Any]]:
        return [call["arguments"] for call in self.calls if call["tool"].endswith("list_sessions")]

    def sends(self) -> list[dict[str, Any]]:
        return [call["arguments"] for call in self.calls if call["tool"].endswith("send_message")]


def _register(temp_db: HubDatabase, external_id: str, agent_type: str, status: str) -> str:
    manager = SessionManager(temp_db)
    session = manager.register(
        external_id=external_id,
        machine_id=require_machine_id(),
        source="codex",
        project_id=PROJECT_ID,
    )
    SessionVariableManager(temp_db).set_variable(session.id, "_agent_type", agent_type)
    if status != "active":
        manager.update_status(session.id, status)
    return session.id


def _executor(temp_db: HubDatabase, proxy: StatusTickProxy) -> PipelineExecutor:
    return PipelineExecutor(
        db=temp_db,
        execution_manager=LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID),
        llm_service=MagicMock(),
        loader=PipelineLoader(temp_db),
        tool_proxy_getter=lambda: proxy,
        template_engine=TemplateEngine(),
        session_manager=SessionManager(temp_db),
    )


async def _tick(temp_db: HubDatabase, proxy: StatusTickProxy) -> Any:
    sync_bundled_pipelines(temp_db)
    pipeline = await PipelineLoader(temp_db).load_pipeline(PIPELINE)
    assert pipeline is not None
    return await _executor(temp_db, proxy).execute(pipeline, {}, PROJECT_ID)


def _step_statuses(temp_db: HubDatabase, execution_id: str) -> dict[str, StepStatus]:
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    return {step.step_id: step.status for step in manager.get_steps_for_execution(execution_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIVE_STATUSES)
async def test_single_live_assistant_is_woken_by_session_id(
    temp_db: HubDatabase, status: str
) -> None:
    _register(temp_db, "old-assistant", "assistant", "expired")
    live_id = _register(temp_db, "live-assistant", "assistant", status)
    proxy = StatusTickProxy(temp_db)

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    listings = proxy.listings()
    assert {listing["status"] for listing in listings} == set(LIVE_STATUSES)
    assert all(listing["project_id"] == PROJECT_ID for listing in listings)
    assert all(listing["agent_type"] == "assistant" for listing in listings)
    sends = proxy.sends()
    assert len(sends) == 1
    assert sends[0]["target"] == "session"
    assert sends[0]["target_id"] == live_id
    assert sends[0]["wake"] is True
    assert sends[0]["content"]


@pytest.mark.asyncio
async def test_live_assistant_wakes_behind_historical_rows_past_listing_limit(
    temp_db: HubDatabase,
) -> None:
    live_id = _register(temp_db, "live-assistant", "assistant", "active")
    for index in range(HISTORICAL_ROWS):
        _register(temp_db, f"old-assistant-{index}", "assistant", "expired")
    proxy = StatusTickProxy(temp_db)
    unfiltered = proxy.list_sessions(
        project_id=PROJECT_ID, agent_type="assistant", limit=LISTING_LIMIT
    )
    assert unfiltered["total"] == HISTORICAL_ROWS + 1
    assert live_id not in {row["id"] for row in unfiltered["sessions"]}

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert [send["target_id"] for send in proxy.sends()] == [live_id]


@pytest.mark.asyncio
async def test_no_live_assistant_refuses_without_sending(temp_db: HubDatabase) -> None:
    _register(temp_db, "old-assistant", "assistant", "expired")
    _register(temp_db, "gone-assistant", "assistant", "closed")
    _register(temp_db, "live-dev-manager", "dev-manager", "active")
    proxy = StatusTickProxy(temp_db)

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert proxy.sends() == []
    assert _step_statuses(temp_db, result.id)["wake"] == StepStatus.SKIPPED


@pytest.mark.asyncio
async def test_two_live_assistants_refuse_without_guessing(temp_db: HubDatabase) -> None:
    _register(temp_db, "first", "assistant", "active")
    _register(temp_db, "second", "assistant", "paused")
    proxy = StatusTickProxy(temp_db)

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert proxy.sends() == []
    assert _step_statuses(temp_db, result.id)["wake"] == StepStatus.SKIPPED


@pytest.mark.asyncio
async def test_incomplete_listing_refuses_without_guessing(temp_db: HubDatabase) -> None:
    for index in range(HISTORICAL_ROWS):
        _register(temp_db, f"live-{index}", "assistant", "active")
    proxy = StatusTickProxy(temp_db)

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert proxy.sends() == []
    assert _step_statuses(temp_db, result.id)["wake"] == StepStatus.SKIPPED
