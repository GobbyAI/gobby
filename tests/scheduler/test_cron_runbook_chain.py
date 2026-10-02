"""Cron pipeline identity chain: system -> cron session -> pipeline child -> spawns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.scheduler.executor import CronExecutor, CronSessionError
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob, CronRun
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager, system_session_id
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.definitions import MCPStepConfig, PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_executor import PipelineExecutor

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"
PIPELINE_NAME = "cron-chain"


class RecordingAgentsProxy:
    """Stub gobby-agents proxy that records each call's ambient session and project."""

    def __init__(self) -> None:
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
        project = get_project_context() or {}
        self.calls.append(
            {
                "tool": f"{server}:{tool}",
                "session_id": session_id,
                "ambient_session_id": get_current_session_id(),
                "ambient_project_id": project.get("id"),
            }
        )
        return {"success": True, "run_id": f"run-{len(self.calls)}"}


def _spawn_pipeline() -> PipelineDefinition:
    return PipelineDefinition(
        name=PIPELINE_NAME,
        steps=[
            PipelineStep(
                id=f"spawn_{index}",
                mcp=MCPStepConfig(
                    server="gobby-agents",
                    tool="spawn_agent",
                    arguments={"agent": "developer", "prompt": f"leaf {index}"},
                ),
            )
            for index in (1, 2)
        ],
    )


def _pipeline_executor(
    temp_db: HubDatabase,
    proxy: RecordingAgentsProxy,
    session_manager: SessionManager | None,
) -> PipelineExecutor:
    return PipelineExecutor(
        db=temp_db,
        execution_manager=LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID),
        llm_service=MagicMock(),
        loader=SimpleNamespace(load_pipeline=AsyncMock(return_value=_spawn_pipeline())),
        tool_proxy_getter=lambda: proxy,
        session_manager=session_manager,
    )


def _make_job(storage: CronJobStorage) -> CronJob:
    return storage.create_job(
        project_id=PROJECT_ID,
        name="runbook chain",
        schedule_type="cron",
        action_type="pipeline",
        action_config={"pipeline_name": PIPELINE_NAME},
        cron_expr="0 * * * *",
    )


def _create_run(storage: CronJobStorage, job: CronJob) -> CronRun:
    run = storage.create_run(job.id)
    assert run is not None
    return run


def _execution_count(temp_db: HubDatabase) -> int:
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS n FROM pipeline_executions WHERE pipeline_name = %s",
        (PIPELINE_NAME,),
    )
    assert row is not None
    return int(row["n"])


async def test_cron_chain_identity(temp_db: HubDatabase) -> None:
    storage = CronJobStorage(temp_db)
    session_manager = SessionManager(temp_db)
    proxy = RecordingAgentsProxy()
    executor = CronExecutor(
        storage=storage,
        pipeline_executor=_pipeline_executor(temp_db, proxy, session_manager),
    )
    job = _make_job(storage)
    run = _create_run(storage, job)

    dispatched = await executor.execute(job, run)
    await asyncio.gather(*list(executor._background_tasks))

    assert dispatched.status == "dispatched"
    assert dispatched.pipeline_execution_id is not None
    persisted = storage.get_run(run.id)
    assert persisted is not None
    assert persisted.status == "completed"

    child = session_manager.find_by_external_id(
        external_id=f"pipeline-{dispatched.pipeline_execution_id}",
        project_id=job.project_id,
        source="pipeline",
    )
    assert child is not None
    assert child.parent_session_id is not None
    cron_session = session_manager.get(child.parent_session_id)
    assert cron_session is not None
    assert cron_session.source == "cron"
    assert cron_session.external_id == f"cron-{job.id}-{run.id}-{PIPELINE_NAME}"
    assert cron_session.parent_session_id == system_session_id()
    assert cron_session.project_id == job.project_id

    assert [entry["tool"] for entry in proxy.calls] == [
        "gobby-agents:spawn_agent",
        "gobby-agents:spawn_agent",
    ]
    for entry in proxy.calls:
        assert entry["session_id"] == child.id
        assert entry["ambient_session_id"] == child.id
        assert entry["ambient_project_id"] == job.project_id


@pytest.mark.parametrize("failure", ["missing_manager", "create_failed"])
async def test_cron_session_failure_refuses(temp_db: HubDatabase, failure: str) -> None:
    storage = CronJobStorage(temp_db)
    session_manager: SessionManager | None = None
    if failure == "create_failed":
        session_manager = SessionManager(temp_db)
    proxy = RecordingAgentsProxy()
    executor = CronExecutor(
        storage=storage,
        pipeline_executor=_pipeline_executor(temp_db, proxy, session_manager),
    )
    job = _make_job(storage)
    run = _create_run(storage, job)

    with patch.object(
        SessionManager, "register", side_effect=RuntimeError("session store unavailable")
    ):
        result = await executor.execute(job, run)
        direct_run = _create_run(storage, job)
        with pytest.raises(CronSessionError, match="cron session"):
            await executor._execute_pipeline(job, direct_run)

    assert result.status == "failed"
    assert result.pipeline_execution_id is None
    assert result.error is not None
    assert "cron session" in result.error
    assert not executor._background_tasks
    assert _execution_count(temp_db) == 0
    assert proxy.calls == []
