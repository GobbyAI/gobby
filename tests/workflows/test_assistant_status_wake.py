"""The status-report tick wakes the one live Assistant session and refuses otherwise."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.integration

PROJECT_ID = "00000000-0000-0000-0000-000000000000"
PIPELINE = "assistant-status-wake"
LIVE_STATUSES = ("active", "paused", "interrupted", "awaiting_input", "awaiting_approval")


class StatusTickProxy:
    """Stub gobby-sessions and gobby-agents proxy: answers the listing and records sends."""

    def __init__(self, sessions: list[dict[str, Any]], total: int | None = None) -> None:
        self.sessions = sessions
        self.total = len(sessions) if total is None else total
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
            return {
                "sessions": self.sessions,
                "count": len(self.sessions),
                "total": self.total,
                "limit": arguments["limit"],
            }
        if tool == "send_message":
            return {"success": True, "delivery_status": "sent", "message_ids": ["m-1"]}
        return {"success": True}

    def listings(self) -> list[dict[str, Any]]:
        return [call["arguments"] for call in self.calls if call["tool"].endswith("list_sessions")]

    def sends(self) -> list[dict[str, Any]]:
        return [call["arguments"] for call in self.calls if call["tool"].endswith("send_message")]


def _session(session_id: str, status: str, agent_type: str = "assistant") -> dict[str, Any]:
    return {
        "id": session_id,
        "ref": f"gobby#{session_id}",
        "status": status,
        "title": agent_type,
        "title_source": "definition",
    }


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
    proxy = StatusTickProxy(
        [_session("old-assistant", "expired"), _session("live-assistant", status)]
    )

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert proxy.listings()[0]["project_id"] == PROJECT_ID
    assert proxy.listings()[0]["agent_type"] == "assistant"
    sends = proxy.sends()
    assert len(sends) == 1
    assert sends[0]["target"] == "session"
    assert sends[0]["target_id"] == "live-assistant"
    assert sends[0]["wake"] is True
    assert sends[0]["content"]


@pytest.mark.asyncio
async def test_no_live_assistant_refuses_without_sending(temp_db: HubDatabase) -> None:
    proxy = StatusTickProxy(
        [_session("old-assistant", "expired"), _session("gone-assistant", "archived")]
    )

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert proxy.sends() == []
    assert _step_statuses(temp_db, result.id)["wake"] == StepStatus.SKIPPED


@pytest.mark.asyncio
async def test_two_live_assistants_refuse_without_guessing(temp_db: HubDatabase) -> None:
    proxy = StatusTickProxy([_session("first", "active"), _session("second", "paused")])

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert proxy.sends() == []
    assert _step_statuses(temp_db, result.id)["wake"] == StepStatus.SKIPPED


@pytest.mark.asyncio
async def test_incomplete_listing_refuses_without_guessing(temp_db: HubDatabase) -> None:
    proxy = StatusTickProxy([_session("live", "active")], total=2)

    result = await _tick(temp_db, proxy)

    assert result.status == ExecutionStatus.COMPLETED
    assert proxy.sends() == []
    assert _step_statuses(temp_db, result.id)["wake"] == StepStatus.SKIPPED
