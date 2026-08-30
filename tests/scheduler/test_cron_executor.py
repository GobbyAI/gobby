"""Tests for CronExecutor dispatch logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.sessions import SessionManager, system_session_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: HubDatabase) -> CronJobStorage:
    return CronJobStorage(temp_db)


@pytest.fixture
def executor(cron_storage: CronJobStorage) -> CronExecutor:
    return CronExecutor(storage=cron_storage)


def _make_job(storage: CronJobStorage, action_type: str, action_config: dict) -> CronJob:
    return storage.create_job(
        project_id=PROJECT_ID,
        name=f"Test {action_type}",
        schedule_type="cron",
        action_type=action_type,
        action_config=action_config,
        cron_expr="0 * * * *",
    )


@pytest.mark.asyncio
async def test_shutdown_cancels_background_tasks(executor: CronExecutor) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(wait_forever(), name="cron-background-test")
    executor._track_background_task(task)
    await started.wait()

    await executor.shutdown()

    assert task.cancelled()
    assert not executor._background_tasks


@pytest.mark.asyncio
async def test_execute_shell_success(cron_storage: CronJobStorage, executor: CronExecutor) -> None:
    """Shell action runs command and captures output."""
    job = _make_job(cron_storage, "shell", {"command": "echo", "args": ["hello world"]})
    run = cron_storage.create_run(job.id)

    with patch("gobby.scheduler.executor.record_automation_event") as record_event:
        result = await executor.execute(job, run)

    assert result.status == "completed"
    assert "hello world" in (result.output or "")
    assert record_event.call_args_list == [
        call("cron", "fired"),
        call("cron", "succeeded"),
    ]


@pytest.mark.asyncio
async def test_execute_shell_timeout(cron_storage: CronJobStorage, executor: CronExecutor) -> None:
    """Shell action respects timeout."""
    job = _make_job(
        cron_storage,
        "shell",
        {"command": "sleep", "args": ["10"], "timeout_seconds": 1},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "timed out" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_execute_shell_failure(cron_storage: CronJobStorage, executor: CronExecutor) -> None:
    """Shell action captures non-zero exit code."""
    job = _make_job(
        cron_storage,
        "shell",
        {"command": "false"},  # always exits with 1
    )
    run = cron_storage.create_run(job.id)

    with patch("gobby.scheduler.executor.record_automation_event") as record_event:
        result = await executor.execute(job, run)

    assert result.status == "failed"
    assert result.error is not None
    assert record_event.call_args_list == [
        call("cron", "fired"),
        call("cron", "failed"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_type", "method_name"),
    [
        ("agent_spawn", "_execute_agent_spawn"),
        ("pipeline", "_execute_pipeline"),
        ("handler", "_execute_handler"),
    ],
)
async def test_execute_bounds_long_running_actions(
    cron_storage: CronJobStorage,
    executor: CronExecutor,
    monkeypatch: pytest.MonkeyPatch,
    action_type: str,
    method_name: str,
) -> None:
    """Agent, pipeline, and handler actions are cancelled at the run timeout."""
    cancelled = asyncio.Event()

    async def hang(*args: object) -> object:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(executor.config, "running_timeout_seconds", 0.01)
    monkeypatch.setattr(executor, method_name, hang)
    action_config = {
        "agent_spawn": {"prompt": "hang"},
        "pipeline": {"pipeline_name": "hang"},
        "handler": {"handler": "hang"},
    }[action_type]
    job = _make_job(cron_storage, action_type, action_config)
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert cancelled.is_set()
    assert result.status == "failed"
    assert result.error == f"{action_type} cron action timed out after 0.01s"


@pytest.mark.asyncio
async def test_handler_timeout_override_outlives_global_budget(
    cron_storage: CronJobStorage,
    executor: CronExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeouts: list[float | None] = []

    async def capture_wait_for(action: Any, *, timeout: float | None) -> object:
        seen_timeouts.append(timeout)
        return await action

    async def long_recap(_job: CronJob) -> str:
        return "recap complete"

    monkeypatch.setattr(executor.config, "running_timeout_seconds", 0.01)
    monkeypatch.setattr(asyncio, "wait_for", capture_wait_for)
    executor.register_handler("wiki:recap:project:alpha", long_recap)
    job = _make_job(
        cron_storage,
        "handler",
        {
            "handler": "wiki:recap:project:alpha",
            "timeout_seconds": 0.1,
        },
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "completed"
    assert result.output == "recap complete"
    assert seen_timeouts == [0.1]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, True, "60"])
async def test_handler_timeout_override_rejects_invalid_values(
    cron_storage: CronJobStorage,
    executor: CronExecutor,
    timeout: object,
) -> None:
    called = False

    async def handler(_job: CronJob) -> str:
        nonlocal called
        called = True
        return "unexpected"

    executor.register_handler("wiki:recap:project:alpha", handler)
    job = _make_job(
        cron_storage,
        "handler",
        {
            "handler": "wiki:recap:project:alpha",
            "timeout_seconds": timeout,
        },
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "failed"
    assert result.error == "action_config.timeout_seconds must be a positive finite number"
    assert called is False


@pytest.mark.asyncio
async def test_execute_agent_spawn_no_runner(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """agent_spawn without agent_runner raises error."""
    job = _make_job(
        cron_storage,
        "agent_spawn",
        {"prompt": "test", "provider": "claude"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_agent_spawn_with_mock_runner(
    cron_storage: CronJobStorage,
) -> None:
    """agent_spawn delegates to spawn_agent_impl and reports success."""
    mock_runner = MagicMock()
    executor = CronExecutor(storage=cron_storage, agent_runner=mock_runner)

    job = _make_job(
        cron_storage,
        "agent_spawn",
        {"prompt": "say hello", "provider": "claude", "timeout_seconds": 30},
    )
    run = cron_storage.create_run(job.id)

    mock_result = {"success": True, "run_id": "dddddddd-dddd-4ddd-8ddd-dddddddd0abc"}
    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_spawn:
        result = await executor.execute(job, run)

    assert result.status == "dispatched"
    assert result.agent_run_id == "dddddddd-dddd-4ddd-8ddd-dddddddd0abc"
    assert "run_id=dddddddd-dddd-4ddd-8ddd-dddddddd0abc" in (result.output or "")
    mock_spawn.assert_called_once()


@pytest.mark.asyncio
async def test_execute_agent_spawn_skips_when_daemon_not_ready(
    cron_storage: CronJobStorage,
) -> None:
    """agent_spawn cron does not spawn while daemon startup is incomplete."""
    mock_runner = MagicMock()
    services = MagicMock(startup_ready=False, shutdown_in_progress=False)
    executor = CronExecutor(storage=cron_storage, agent_runner=mock_runner, services=services)
    job = _make_job(cron_storage, "agent_spawn", {"prompt": "say hello"})
    run = cron_storage.create_run(job.id)

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        new_callable=AsyncMock,
    ) as mock_spawn:
        result = await executor.execute(job, run)

    assert result.status == "skipped"
    assert result.output == "Agent spawn skipped: daemon_startup_not_ready"
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_execute_agent_spawn_failure_records_failed_run(
    cron_storage: CronJobStorage,
) -> None:
    """agent_spawn success=false becomes a failed cron run."""
    mock_runner = MagicMock()
    executor = CronExecutor(storage=cron_storage, agent_runner=mock_runner)
    job = _make_job(cron_storage, "agent_spawn", {"prompt": "say hello"})
    run = cron_storage.create_run(job.id)

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        new_callable=AsyncMock,
        return_value={"success": False, "error": "tmux unavailable"},
    ):
        result = await executor.execute(job, run)

    assert result.status == "failed"
    assert result.error == "Agent spawn failed: tmux unavailable"


@pytest.mark.asyncio
async def test_execute_agent_spawn_success_without_run_id_fails(
    cron_storage: CronJobStorage,
) -> None:
    """agent_spawn success requires a structured run_id."""
    mock_runner = MagicMock()
    executor = CronExecutor(storage=cron_storage, agent_runner=mock_runner)
    job = _make_job(cron_storage, "agent_spawn", {"prompt": "say hello"})
    run = cron_storage.create_run(job.id)

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        new_callable=AsyncMock,
        return_value={"success": True},
    ):
        result = await executor.execute(job, run)

    assert result.status == "failed"
    assert "structured run_id" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_pipeline_no_executor(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """pipeline without pipeline_executor raises error."""
    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "test-pipeline"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_pipeline_disabled_is_skipped_without_side_effects(
    cron_storage: CronJobStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pipeline = SimpleNamespace(name="disabled-pipeline", enabled=False)
    load_pipeline = AsyncMock(return_value=pipeline)
    session_manager = MagicMock()
    execution_manager = MagicMock()
    execute_pipeline = AsyncMock()
    pipeline_executor = SimpleNamespace(
        loader=SimpleNamespace(load_pipeline=load_pipeline),
        session_manager=session_manager,
        execution_manager=execution_manager,
        execute=execute_pipeline,
    )
    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)
    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "disabled-pipeline"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "skipped"
    assert result.output == "Skipped: pipeline 'disabled-pipeline' is disabled"
    assert result.error is None
    assert result.pipeline_execution_id is None
    load_pipeline.assert_awaited_once_with("disabled-pipeline", job.project_id)
    session_manager.register.assert_not_called()
    execution_manager.create_execution.assert_not_called()
    execute_pipeline.assert_not_awaited()
    assert not executor._background_tasks
    assert all(record.levelname != "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_execute_pipeline_missing_target_still_fails(
    cron_storage: CronJobStorage,
) -> None:
    load_pipeline = AsyncMock(return_value=None)
    pipeline_executor = SimpleNamespace(
        loader=SimpleNamespace(load_pipeline=load_pipeline),
        session_manager=MagicMock(),
        execution_manager=MagicMock(),
        execute=AsyncMock(),
    )
    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)
    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "missing-pipeline"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "failed"
    assert result.error == "Pipeline 'missing-pipeline' not found"


@pytest.mark.asyncio
async def test_execute_pipeline_resolves_executor_for_job_project(
    cron_storage: CronJobStorage,
) -> None:
    pipeline = SimpleNamespace(
        name="cron-test-pipeline",
        enabled=True,
        model_dump_json=lambda: '{"name":"cron-test-pipeline"}',
    )
    load_pipeline = AsyncMock(return_value=pipeline)
    create_execution = MagicMock(
        return_value=SimpleNamespace(id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0200")
    )
    execute_pipeline = AsyncMock(return_value=None)
    pipeline_executor = SimpleNamespace(
        loader=SimpleNamespace(load_pipeline=load_pipeline),
        session_manager=None,
        execution_manager=SimpleNamespace(create_execution=create_execution),
        execute=execute_pipeline,
    )

    resolved_projects: list[str] = []

    def get_pipeline_executor(project_id: str) -> SimpleNamespace:
        resolved_projects.append(project_id)
        return pipeline_executor

    services = SimpleNamespace(get_pipeline_executor=get_pipeline_executor)
    executor = CronExecutor(storage=cron_storage, services=services)
    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "cron-test-pipeline"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    if executor._background_tasks:
        await asyncio.gather(*list(executor._background_tasks), return_exceptions=True)

    assert result.status == "dispatched"
    assert resolved_projects == [job.project_id]
    load_pipeline.assert_awaited_once_with("cron-test-pipeline", job.project_id)
    execute_pipeline.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_pipeline_recreates_missing_system_session(
    cron_storage: CronJobStorage, temp_db: HubDatabase
) -> None:
    """Pipeline cron runs repair the root system session before creating cron sessions."""
    pipeline = MagicMock()
    pipeline.name = "cron-test-pipeline"
    pipeline.model_dump_json.return_value = '{"name":"cron-test-pipeline"}'

    pipeline_executor = MagicMock()
    pipeline_executor.loader = MagicMock()
    pipeline_executor.loader.load_pipeline = AsyncMock(return_value=pipeline)
    pipeline_executor.session_manager = SessionManager(temp_db)

    execution = MagicMock()
    execution.id = "eeeeeeee-eeee-4eee-8eee-eeeeeeee0201"
    pipeline_executor.execution_manager = MagicMock()
    pipeline_executor.execution_manager.create_execution.return_value = execution
    pipeline_executor.execute = AsyncMock(return_value=None)

    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)

    temp_db.execute("DELETE FROM sessions WHERE id = %s", (system_session_id(),))
    assert temp_db.fetchone("SELECT id FROM sessions WHERE id = %s", (system_session_id(),)) is None

    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "cron-test-pipeline"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    if executor._background_tasks:
        await asyncio.gather(*list(executor._background_tasks), return_exceptions=True)

    assert result.status == "dispatched"
    assert result.pipeline_execution_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeee0201"
    repaired = temp_db.fetchone("SELECT id FROM sessions WHERE id = %s", (system_session_id(),))
    assert repaired is not None

    cron_session_id = pipeline_executor.execute.await_args.kwargs["session_id"]
    cron_session = temp_db.fetchone(
        "SELECT source, parent_session_id FROM sessions WHERE id = %s",
        (cron_session_id,),
    )
    assert cron_session is not None
    assert cron_session["source"] == "cron"
    assert cron_session["parent_session_id"] == system_session_id()
    pipeline_executor.loader.load_pipeline.assert_awaited_once_with(
        "cron-test-pipeline", job.project_id
    )


@pytest.mark.asyncio
async def test_execute_pipeline_background_success_completes_cron_run(
    cron_storage: CronJobStorage,
) -> None:
    pipeline = MagicMock()
    pipeline.name = "cron-success"
    pipeline.model_dump_json.return_value = '{"name":"cron-success"}'

    pipeline_executor = MagicMock()
    pipeline_executor.loader = MagicMock()
    pipeline_executor.loader.load_pipeline = AsyncMock(return_value=pipeline)
    pipeline_executor.session_manager = None
    execution = MagicMock()
    execution.id = "eeeeeeee-eeee-4eee-8eee-eeeeeeee0205"
    pipeline_executor.execution_manager = MagicMock()
    pipeline_executor.execution_manager.create_execution.return_value = execution
    pipeline_executor.execute = AsyncMock(return_value=None)

    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)
    job = _make_job(cron_storage, "pipeline", {"pipeline_name": "cron-success"})
    run = cron_storage.create_run(job.id)

    with patch("gobby.scheduler.executor.record_automation_event") as record_event:
        dispatched = await executor.execute(job, run)
        await asyncio.gather(*list(executor._background_tasks))

    persisted = cron_storage.get_run(run.id)
    assert dispatched.status == "dispatched"
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.error is None
    assert persisted.completed_at is not None
    assert persisted.pipeline_execution_id == execution.id
    assert record_event.call_args_list == [
        call("cron", "fired"),
        call("cron", "succeeded"),
    ]


@pytest.mark.asyncio
async def test_execute_pipeline_background_failure_fails_cron_run(
    cron_storage: CronJobStorage,
) -> None:
    error = "pipeline exploded:" + "p" * 7_000
    pipeline = MagicMock()
    pipeline.name = "cron-failure"
    pipeline.model_dump_json.return_value = '{"name":"cron-failure"}'

    pipeline_executor = MagicMock()
    pipeline_executor.loader = MagicMock()
    pipeline_executor.loader.load_pipeline = AsyncMock(return_value=pipeline)
    pipeline_executor.session_manager = None
    execution = MagicMock()
    execution.id = "eeeeeeee-eeee-4eee-8eee-eeeeeeee0206"
    pipeline_executor.execution_manager = MagicMock()
    pipeline_executor.execution_manager.create_execution.return_value = execution
    pipeline_executor.execution_manager.get_steps_for_execution.return_value = []
    pipeline_executor.execute = AsyncMock(side_effect=RuntimeError(error))

    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)
    job = _make_job(cron_storage, "pipeline", {"pipeline_name": "cron-failure"})
    run = cron_storage.create_run(job.id)

    with patch("gobby.scheduler.executor.record_automation_event") as record_event:
        dispatched = await executor.execute(job, run)
        await asyncio.gather(*list(executor._background_tasks))

    persisted = cron_storage.get_run(run.id)
    assert dispatched.status == "dispatched"
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.error == error
    assert persisted.completed_at is not None
    assert persisted.pipeline_execution_id == execution.id
    assert record_event.call_args_list == [
        call("cron", "fired"),
        call("cron", "failed"),
    ]


@pytest.mark.asyncio
async def test_execute_pipeline_background_timeout_fails_cron_run(
    cron_storage: CronJobStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung background pipeline is cancelled and terminalizes its cron run."""
    pipeline = MagicMock()
    pipeline.name = "cron-timeout"
    pipeline.model_dump_json.return_value = '{"name":"cron-timeout"}'

    pipeline_executor = MagicMock()
    pipeline_executor.loader = MagicMock()
    pipeline_executor.loader.load_pipeline = AsyncMock(return_value=pipeline)
    pipeline_executor.session_manager = None
    execution = MagicMock()
    execution.id = "eeeeeeee-eeee-4eee-8eee-eeeeeeee0207"
    pipeline_executor.execution_manager = MagicMock()
    pipeline_executor.execution_manager.create_execution.return_value = execution
    pipeline_executor.execution_manager.get_steps_for_execution.return_value = []

    cancelled = asyncio.Event()

    async def hang(**kwargs: object) -> object:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    pipeline_executor.execute = AsyncMock(side_effect=hang)
    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)
    monkeypatch.setattr(executor.config, "running_timeout_seconds", 0.01)
    job = _make_job(cron_storage, "pipeline", {"pipeline_name": "cron-timeout"})
    run = cron_storage.create_run(job.id)

    dispatched = await executor.execute(job, run)
    await asyncio.gather(*list(executor._background_tasks))

    persisted = cron_storage.get_run(run.id)
    assert cancelled.is_set()
    assert dispatched.status == "dispatched"
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.error == "pipeline cron action timed out after 0.01s"
    assert persisted.completed_at is not None
    assert persisted.pipeline_execution_id == execution.id


@pytest.mark.asyncio
async def test_execute_pipeline_default_overlap_skips_active_child(
    cron_storage: CronJobStorage,
) -> None:
    """Pipeline cron skips when a previous dispatched child is still active."""
    job = _make_job(cron_storage, "pipeline", {"pipeline_name": "approval"})
    previous = cron_storage.create_run(job.id)
    assert previous is not None
    cron_storage.db.execute(
        """
        INSERT INTO pipeline_executions (id, pipeline_name, project_id, status)
        VALUES (%s, %s, %s, %s)
        """,
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeee0202", "approval", PROJECT_ID, "waiting_approval"),
    )
    cron_storage.update_run(
        previous.id,
        status="dispatched",
        pipeline_execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0202",
        completed_at="2026-02-10T00:00:00+00:00",
    )
    run = cron_storage.create_run(job.id)
    assert run is not None
    executor = CronExecutor(storage=cron_storage)

    result = await executor.execute(job, run)

    assert result.status == "skipped"
    assert "active child pipeline_execution eeeeeeee-eeee-4eee-8eee-eeeeeeee0202" in (
        result.output or ""
    )


@pytest.mark.asyncio
async def test_execute_pipeline_overlap_allow_launches_another_child(
    cron_storage: CronJobStorage,
) -> None:
    """overlap_policy=allow bypasses active child overlap checks."""
    pipeline = MagicMock()
    pipeline.name = "approval"
    pipeline.model_dump_json.return_value = '{"name":"approval"}'
    pipeline_executor = MagicMock()
    pipeline_executor.loader = MagicMock()
    pipeline_executor.loader.load_pipeline = AsyncMock(return_value=pipeline)
    pipeline_executor.session_manager = None
    execution = MagicMock()
    execution.id = "eeeeeeee-eeee-4eee-8eee-eeeeeeee0203"
    pipeline_executor.execution_manager = MagicMock()
    pipeline_executor.execution_manager.create_execution.return_value = execution
    pipeline_executor.execute = AsyncMock(return_value=None)
    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)
    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "approval", "overlap_policy": "allow"},
    )
    previous = cron_storage.create_run(job.id)
    assert previous is not None
    cron_storage.db.execute(
        """
        INSERT INTO pipeline_executions (id, pipeline_name, project_id, status)
        VALUES (%s, %s, %s, %s)
        """,
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeee0204", "approval", PROJECT_ID, "running"),
    )
    cron_storage.update_run(
        previous.id,
        status="dispatched",
        pipeline_execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0204",
        completed_at="2026-02-10T00:00:00+00:00",
    )
    run = cron_storage.create_run(job.id)
    assert run is not None

    result = await executor.execute(job, run)
    if executor._background_tasks:
        await asyncio.gather(*list(executor._background_tasks), return_exceptions=True)

    assert result.status == "dispatched"
    assert result.pipeline_execution_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeee0203"
    pipeline_executor.loader.load_pipeline.assert_awaited_once_with("approval", job.project_id)


@pytest.mark.asyncio
async def test_execute_agent_spawn_invalid_overlap_policy_fails(
    cron_storage: CronJobStorage,
) -> None:
    """Invalid overlap policy is recorded as a failed cron run."""
    executor = CronExecutor(storage=cron_storage, agent_runner=MagicMock())
    job = _make_job(
        cron_storage,
        "agent_spawn",
        {"prompt": "hello", "overlap_policy": "sometimes"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "failed"
    assert "Invalid overlap_policy" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_unknown_action_type(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Unknown action_type returns error."""
    job = _make_job(cron_storage, "shell", {"command": "echo"})
    # Hack action_type to something invalid
    job.action_type = "unknown"
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "Unknown action_type" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_updates_run_status(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Execute updates run to 'completed' and clears stale errors."""
    job = _make_job(cron_storage, "shell", {"command": "echo", "args": ["test"]})
    run = cron_storage.create_run(job.id)
    cron_storage.update_run(run.id, error="Cron run exceeded running timeout (60s)")
    assert run.status == "pending"

    await executor.execute(job, run)
    # Fetch fresh from DB
    final = cron_storage.get_run(run.id)
    assert final is not None
    assert final.status == "completed"
    assert final.started_at is not None
    assert final.completed_at is not None
    assert final.error is None


@pytest.mark.asyncio
async def test_execute_shell_missing_command(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Shell action without command in config returns error."""
    job = _make_job(cron_storage, "shell", {"args": ["hello"]})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "command" in (result.error or "").lower()


# --- Handler action type tests ---


@pytest.mark.asyncio
async def test_execute_handler_success(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Handler action dispatches to registered callable."""

    async def my_handler(job: CronJob) -> str:
        return f"handled: {job.name}"

    executor.register_handler("test_handler", my_handler)
    job = _make_job(cron_storage, "handler", {"handler": "test_handler"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "completed"
    assert "handled: Test handler" in (result.output or "")


@pytest.mark.asyncio
async def test_execute_preserves_oversized_handler_output(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    output = "output:" + "x" * 12_000

    async def oversized_handler(_job: CronJob) -> str:
        return output

    executor.register_handler("oversized_output", oversized_handler)
    job = _make_job(cron_storage, "handler", {"handler": "oversized_output"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "completed"
    assert result.output == output


@pytest.mark.asyncio
async def test_execute_handler_missing_name(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Handler action without handler name in config returns error."""
    job = _make_job(cron_storage, "handler", {"some_key": "value"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "handler" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_execute_handler_unregistered(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Handler action with unregistered handler name returns error."""
    job = _make_job(cron_storage, "handler", {"handler": "nonexistent"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "No handler registered" in (result.error or "")
    assert "nonexistent" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_handler_error_propagates(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Handler that raises an exception results in failed run."""

    async def failing_handler(job: CronJob) -> str:
        raise RuntimeError("handler exploded")

    executor.register_handler("boom", failing_handler)
    job = _make_job(cron_storage, "handler", {"handler": "boom"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "handler exploded" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_preserves_oversized_handler_error(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    error = "error:" + "y" * 7_000

    async def failing_handler(_job: CronJob) -> str:
        raise RuntimeError(error)

    executor.register_handler("oversized_error", failing_handler)
    job = _make_job(cron_storage, "handler", {"handler": "oversized_error"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "failed"
    assert result.error == error


@pytest.mark.asyncio
async def test_execute_handler_mapping_failure_result_records_failed_run(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Handler success=false mapping results fail the cron run."""

    async def failing_handler(job: CronJob) -> dict[str, object]:
        return {"success": False, "error": "handler reported failure"}

    executor.register_handler("mapping_failure", failing_handler)
    job = _make_job(cron_storage, "handler", {"handler": "mapping_failure"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "failed"
    assert result.error == "handler reported failure"


@pytest.mark.asyncio
async def test_execute_handler_json_failure_result_records_failed_run(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Handler JSON object strings with ok=false fail the cron run."""

    async def failing_handler(job: CronJob) -> str:
        return '{"ok": false, "error": "json failure"}'

    executor.register_handler("json_failure", failing_handler)
    job = _make_job(cron_storage, "handler", {"handler": "json_failure"})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "failed"
    assert result.error == "json failure"


# --- agent_definition resolution tests ---


@pytest.mark.asyncio
async def test_execute_agent_spawn_with_agent_definition(
    cron_storage: CronJobStorage,
) -> None:
    """agent_spawn with agent_definition prepends preamble to prompt and uses its provider."""
    mock_runner = MagicMock()
    executor = CronExecutor(storage=cron_storage, agent_runner=mock_runner)

    job = _make_job(
        cron_storage,
        "agent_spawn",
        {
            "prompt": "Fix the bug",
            "agent_definition": "test-agent",
        },
    )
    run = cron_storage.create_run(job.id)

    # Mock resolve_agent to return an agent with preamble
    mock_body = MagicMock()
    mock_body.prompt_for.return_value = "## Agent\nYou are a developer"
    mock_body.provider = "qwen"

    mock_result = {"success": True, "run_id": "dddddddd-dddd-4ddd-8ddd-dddddddd0def"}
    with (
        patch("gobby.workflows.agent_resolver.resolve_agent", return_value=mock_body),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_spawn,
    ):
        result = await executor.execute(job, run)

    assert result.status == "dispatched"
    call_kwargs = mock_spawn.call_args
    # Check preamble was prepended to prompt
    prompt = call_kwargs.kwargs.get("prompt", "")
    assert "## Agent" in prompt
    mock_body.prompt_for.assert_called_once_with("agent")
    assert "Fix the bug" in prompt
    # Provider from agent definition should be used (no explicit provider in config)
    assert call_kwargs.kwargs.get("provider") == "qwen"


@pytest.mark.asyncio
async def test_execute_agent_spawn_agent_definition_not_found(
    cron_storage: CronJobStorage,
) -> None:
    """agent_spawn continues without preamble if agent_definition not found."""
    mock_runner = MagicMock()
    executor = CronExecutor(storage=cron_storage, agent_runner=mock_runner)

    job = _make_job(
        cron_storage,
        "agent_spawn",
        {
            "prompt": "Do stuff",
            "agent_definition": "nonexistent-agent",
        },
    )
    run = cron_storage.create_run(job.id)

    mock_result = {"success": True, "run_id": "dddddddd-dddd-4ddd-8ddd-dddddddd0987"}
    with (
        patch("gobby.workflows.agent_resolver.resolve_agent", return_value=None),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_spawn,
    ):
        result = await executor.execute(job, run)

    assert result.status == "dispatched"
    # Prompt should be unchanged (no preamble)
    call_kwargs = mock_spawn.call_args
    assert call_kwargs.kwargs.get("prompt") == "Do stuff"


@pytest.mark.asyncio
async def test_agent_spawn_supplies_owning_completion_registry(
    cron_storage: CronJobStorage,
) -> None:
    """Plan 1.4.10: the cron surface passes its registry into spawn_agent_impl,
    so the deferred health check can wake a pre-registered waiter."""
    from types import SimpleNamespace

    registry = object()
    executor = CronExecutor(
        storage=cron_storage,
        agent_runner=MagicMock(),
        services=SimpleNamespace(completion_registry=registry),
    )
    job = _make_job(
        cron_storage,
        "agent_spawn",
        {"prompt": "say hello", "provider": "claude", "timeout_seconds": 30},
    )
    run = cron_storage.create_run(job.id)

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        new_callable=AsyncMock,
        return_value={"success": True, "run_id": "dddddddd-dddd-4ddd-8ddd-dddddddd0abc"},
    ) as mock_spawn:
        result = await executor.execute(job, run)

    assert result.status == "dispatched"
    assert mock_spawn.call_args.kwargs["completion_registry"] is registry


def test_pipeline_project_context_uses_machine_checkout(  # tdd-red window
    cron_storage: CronJobStorage,
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path as PathType

    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    executor = CronExecutor(storage=cron_storage)

    ctx = executor._pipeline_project_context(isolated.project.id)

    assert ctx["id"] == isolated.project.id
    assert ctx["project_path"] == isolated.root_path
    assert PathType(ctx["project_path"]).exists()


def test_pipeline_project_context_fails_closed_without_checkout(  # tdd-red window
    cron_storage: CronJobStorage,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from gobby.storage.projects import LocalProjectManager
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="cron-no-checkout")
    executor = CronExecutor(storage=cron_storage)

    with pytest.raises(CheckoutNotFoundError):
        executor._pipeline_project_context(project.id)
