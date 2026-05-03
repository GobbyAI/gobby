"""Phase 2 tests for the canonical in-process expansion entry point."""

from __future__ import annotations

import asyncio
from inspect import signature
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


def _task(task_manager: LocalTaskManager, sample_project):
    return task_manager.create_task(project_id=sample_project["id"], title="Expand me")


def test_reset_expansion_output_tool_is_registered(temp_db) -> None:
    from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

    registry = create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        config=MagicMock(),
    )

    assert any(item["name"] == "reset_expansion_output" for item in registry.list_tools())
    schema = registry.get_schema("reset_expansion_output")["inputSchema"]
    assert schema["required"] == ["task_id"]
    assert schema["properties"]["run_id"]["type"] == "string"


def test_start_expansion_schema_accepts_reset_output(temp_db) -> None:
    from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

    registry = create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        config=MagicMock(),
    )

    schema = registry.get_schema("start_expansion_run")["inputSchema"]
    assert schema["properties"]["reset_output"]["type"] == "boolean"


def test_start_expansion_idempotent(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    run = LocalExpansionRunManager(temp_db).create(
        parent_task_id=task.id,
        project_id=task.project_id,
        triggering_session_id=None,
        input_source="task",
    )

    result = start_expansion_run_impl(
        task_manager=task_manager,
        llm_service=MagicMock(),
        config=MagicMock(),
        completion_registry=MagicMock(),
        triggering_session_id=None,
        task_id=task.id,
    )

    assert result.reused is True
    assert result.run_id == run.id


def test_completion_emits_terminal_event(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    run_manager = LocalExpansionRunManager(temp_db)
    registry = MagicMock()

    async def complete_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
    ):
        _ = self, session_id, auto_apply
        return run_manager.save_apply_result(run_id, task_id_map={}, created_task_ids=[])

    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        new=complete_run,
    ):
        start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=registry,
            triggering_session_id=None,
            task_id=task.id,
            run_id="run-complete",
        )

    registry.emit.assert_any_call("expansion_run_completed", task_id=task.id, run_id="run-complete")


def test_failure_emits_terminal_event(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    registry = MagicMock()

    async def fail_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
    ):
        _ = self, run_id, session_id, auto_apply
        raise RuntimeError("boom")

    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        new=fail_run,
    ):
        start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=registry,
            triggering_session_id=None,
            task_id=task.id,
            run_id="run-failed",
        )

    registry.emit.assert_any_call(
        "expansion_run_failed",
        task_id=task.id,
        run_id="run-failed",
        reason="boom",
    )


def test_cancellation_emits_terminal_event(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    registry = MagicMock()

    async def cancel_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
    ):
        _ = self, run_id, session_id, auto_apply
        raise asyncio.CancelledError()

    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        new=cancel_run,
    ):
        start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=registry,
            triggering_session_id=None,
            task_id=task.id,
            run_id="run-cancelled",
        )

    registry.emit.assert_any_call(
        "expansion_run_cancelled", task_id=task.id, run_id="run-cancelled"
    )


def test_start_expansion_accepts_caller_allocated_run_id(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    run_manager = LocalExpansionRunManager(temp_db)

    async def complete_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
    ):
        _ = self, session_id, auto_apply
        return run_manager.save_apply_result(run_id, task_id_map={}, created_task_ids=[])

    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        new=complete_run,
    ):
        result = start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=MagicMock(),
            triggering_session_id=None,
            task_id=task.id,
            run_id="caller-run-id",
        )

    assert "run_id" in signature(start_expansion_run_impl).parameters
    assert result.run_id == "caller-run-id"
    assert run_manager.get("caller-run-id") is not None


def test_start_expansion_reset_output_calls_reset(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    run_manager = LocalExpansionRunManager(temp_db)

    async def complete_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
    ):
        _ = self, session_id, auto_apply
        return run_manager.save_apply_result(run_id, task_id_map={}, created_task_ids=[])

    with (
        patch(
            "gobby.tasks.expansion_service.ExpansionService.reset_expansion_output",
            autospec=True,
        ) as reset,
        patch(
            "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
            new=complete_run,
        ),
    ):
        result = start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=MagicMock(),
            triggering_session_id=None,
            task_id=task.id,
            run_id="reset-run",
            reset_output=True,
        )

    assert result.run_id == "reset-run"
    reset.assert_called_once()


def test_synchronous_terminal_emits_event(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    run_manager = LocalExpansionRunManager(temp_db)
    registry = MagicMock()

    async def complete_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
    ):
        _ = self, session_id, auto_apply
        return run_manager.save_apply_result(run_id, task_id_map={}, created_task_ids=[])

    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        new=complete_run,
    ):
        result = start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=registry,
            triggering_session_id=None,
            task_id=task.id,
            run_id="run-sync",
            auto_apply=True,
        )

    assert result.status == "completed"
    registry.emit.assert_any_call("expansion_run_completed", task_id=task.id, run_id="run-sync")


@pytest.mark.asyncio
async def test_async_start_returns_running_and_emits_later(temp_db, sample_project) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    run_manager = LocalExpansionRunManager(temp_db)
    registry = MagicMock()

    async def complete_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
    ):
        _ = self, session_id, auto_apply
        await asyncio.sleep(0)
        return run_manager.save_apply_result(run_id, task_id_map={}, created_task_ids=[])

    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        new=complete_run,
    ):
        result = start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=registry,
            triggering_session_id=None,
            task_id=task.id,
            run_id="run-async",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result.status == "running"
    registry.emit.assert_any_call("expansion_run_completed", task_id=task.id, run_id="run-async")
