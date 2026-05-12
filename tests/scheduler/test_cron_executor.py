"""Tests for CronExecutor dispatch logic."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.sessions import SYSTEM_SESSION_ID, SessionManager

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: LocalDatabase) -> CronJobStorage:
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
async def test_execute_shell_success(cron_storage: CronJobStorage, executor: CronExecutor) -> None:
    """Shell action runs command and captures output."""
    job = _make_job(cron_storage, "shell", {"command": "echo", "args": ["hello world"]})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "completed"
    assert "hello world" in (result.output or "")


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

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert result.error is not None


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

    mock_result = {"success": True, "run_id": "run-abc123"}
    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_spawn:
        result = await executor.execute(job, run)

    assert result.status == "completed"
    assert "run_id=run-abc123" in (result.output or "")
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

    assert result.status == "completed"
    assert result.output == "Agent spawn skipped: daemon_startup_not_ready"
    mock_spawn.assert_not_called()


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
async def test_execute_pipeline_recreates_missing_system_session(
    cron_storage: CronJobStorage, temp_db: LocalDatabase
) -> None:
    """Pipeline cron runs repair the root system session before creating cron sessions."""
    pipeline = MagicMock()
    pipeline.name = "nightly-memory-cleanup"

    pipeline_executor = MagicMock()
    pipeline_executor.loader = MagicMock()
    pipeline_executor.loader.load_pipeline = AsyncMock(return_value=pipeline)
    pipeline_executor.session_manager = SessionManager(temp_db)

    execution = MagicMock()
    execution.id = "pe-cron-123"
    execution.status = "completed"
    pipeline_executor.execute = AsyncMock(return_value=execution)

    executor = CronExecutor(storage=cron_storage, pipeline_executor=pipeline_executor)

    temp_db.execute("DELETE FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,))
    assert temp_db.fetchone("SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,)) is None

    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "nightly-memory-cleanup"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)

    assert result.status == "completed"
    repaired = temp_db.fetchone("SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,))
    assert repaired is not None

    cron_session_id = pipeline_executor.execute.await_args.kwargs["session_id"]
    cron_session = temp_db.fetchone(
        "SELECT source, parent_session_id FROM sessions WHERE id = ?",
        (cron_session_id,),
    )
    assert cron_session is not None
    assert cron_session["source"] == "cron"
    assert cron_session["parent_session_id"] == SYSTEM_SESSION_ID


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
    """Execute updates run to 'running' then 'completed'."""
    job = _make_job(cron_storage, "shell", {"command": "echo", "args": ["test"]})
    run = cron_storage.create_run(job.id)
    assert run.status == "pending"

    await executor.execute(job, run)
    # Fetch fresh from DB
    final = cron_storage.get_run(run.id)
    assert final is not None
    assert final.status == "completed"
    assert final.started_at is not None
    assert final.completed_at is not None


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
    mock_body.build_prompt_preamble.return_value = "## Role\nYou are a developer"
    mock_body.provider = "gemini"

    mock_result = {"success": True, "run_id": "run-def456"}
    with (
        patch("gobby.workflows.agent_resolver.resolve_agent", return_value=mock_body),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_spawn,
    ):
        result = await executor.execute(job, run)

    assert result.status == "completed"
    call_kwargs = mock_spawn.call_args
    # Check preamble was prepended to prompt
    prompt = call_kwargs.kwargs.get("prompt", "")
    assert "## Role" in prompt
    assert "Fix the bug" in prompt
    # Provider from agent definition should be used (no explicit provider in config)
    assert call_kwargs.kwargs.get("provider") == "gemini"


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

    mock_result = {"success": True, "run_id": "run-ghi789"}
    with (
        patch("gobby.workflows.agent_resolver.resolve_agent", return_value=None),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_spawn,
    ):
        result = await executor.execute(job, run)

    assert result.status == "completed"
    # Prompt should be unchanged (no preamble)
    call_kwargs = mock_spawn.call_args
    assert call_kwargs.kwargs.get("prompt") == "Do stuff"
