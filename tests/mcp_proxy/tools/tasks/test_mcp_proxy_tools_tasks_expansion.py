"""Phase 2 tests for the canonical in-process expansion entry point."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from inspect import signature
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.expansion_runs import ExpansionRun, LocalExpansionRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.tasks.expansion_service import ExpansionService
from gobby.utils.datetime import utc_now
from tests._timing import drain_asyncio_tasks
from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

pytestmark = pytest.mark.unit


def _task(task_manager: LocalTaskManager, sample_project: dict[str, Any]) -> Task:
    return task_manager.create_task(
        project_id=sample_project["id"],
        title="Expand me",
        validation_criteria="Test task completion is observable.",
    )


def _complete_run(run_manager: LocalExpansionRunManager, run_id: str) -> ExpansionRun:
    run_manager.db.execute(
        "UPDATE expansion_runs SET status = 'applying' WHERE id = %s",
        (run_id,),
    )
    result = run_manager.save_apply_result(run_id, task_id_map={}, created_task_ids=[])
    assert result is not None
    return result


def test_reset_expansion_output_tool_is_registered(temp_db) -> None:
    from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

    registry = create_task_ops_registry(
        LocalTaskManager(temp_db),
        startup_config=MagicMock(),
    )

    assert any(item["name"] == "reset_expansion_output" for item in registry.list_tools())
    schema = registry.get_schema("reset_expansion_output")["inputSchema"]
    assert schema["required"] == ["task_id"]
    assert schema["properties"]["run_id"]["type"] == "string"


def test_start_expansion_schema_accepts_reset_output(temp_db) -> None:
    from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

    registry = create_task_ops_registry(
        LocalTaskManager(temp_db),
        startup_config=MagicMock(),
    )

    schema = registry.get_schema("start_expansion_run")["inputSchema"]
    assert schema["properties"]["reset_output"]["type"] == "boolean"
    assert schema["properties"]["stage_pipeline_mode"]["type"] == ["boolean", "null"]


def test_start_expansion_schema_accepts_explicit_null_optionals(temp_db) -> None:
    """The expand-task pipeline sends explicit nulls for unset optional inputs."""
    from gobby.mcp_proxy.services.argument_validation import check_arguments
    from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

    registry = create_task_ops_registry(
        LocalTaskManager(temp_db),
        startup_config=MagicMock(),
    )

    schema = registry.get_schema("start_expansion_run")["inputSchema"]
    errors = check_arguments(
        {
            "task_id": "#1",
            "plan_file": None,
            "provider": None,
            "model": None,
            "project": None,
            "auto_apply": True,
        },
        schema,
    )
    assert errors == []


def test_start_expansion_idempotent(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
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


def test_start_expansion_replaces_stale_crashed_run(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = _task(task_manager, sample_project)
    run_manager = LocalExpansionRunManager(temp_db)
    crashed = run_manager.create(
        parent_task_id=task.id,
        project_id=task.project_id,
        triggering_session_id=None,
        input_source="task",
    )
    temp_db.execute(
        "UPDATE expansion_runs SET status = 'running', updated_at = %s WHERE id = %s",
        (utc_now() - timedelta(minutes=31), crashed.id),
    )

    def finish_immediately(coro: object) -> ExpansionRun | None:
        coro.close()
        return run_manager.get_latest_for_task(task.id)

    with patch(
        "gobby.mcp_proxy.tools.tasks._expansion_runtime._run_start_coroutine",
        side_effect=finish_immediately,
    ):
        result = start_expansion_run_impl(
            task_manager=task_manager,
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=MagicMock(),
            triggering_session_id=None,
            task_id=task.id,
        )

    assert result.reused is False
    assert result.run_id != crashed.id
    assert run_manager.get(crashed.id).status == "failed"


def test_completion_emits_terminal_event(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, session_id, auto_apply, suppress_parent_stage_transition
        return _complete_run(run_manager, run_id)

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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4006",
        )

    registry.emit.assert_any_call(
        "expansion_run_completed", task_id=task.id, run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4006"
    )


def test_failure_emits_terminal_event(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
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
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, run_id, session_id, auto_apply, suppress_parent_stage_transition
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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4004",
        )

    registry.emit.assert_any_call(
        "expansion_run_failed",
        task_id=task.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4004",
        reason="boom",
    )


def test_cancellation_emits_terminal_event(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, run_id, session_id, auto_apply, suppress_parent_stage_transition
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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4007",
        )

    registry.emit.assert_any_call(
        "expansion_run_cancelled", task_id=task.id, run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4007"
    )


def test_start_expansion_accepts_caller_allocated_run_id(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, session_id, auto_apply, suppress_parent_stage_transition
        return _complete_run(run_manager, run_id)

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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd400d",
        )

    assert "run_id" in signature(start_expansion_run_impl).parameters
    assert result.run_id == "dddddddd-dddd-4ddd-8ddd-dddddddd400d"
    assert run_manager.get("dddddddd-dddd-4ddd-8ddd-dddddddd400d") is not None


def test_start_expansion_reset_output_calls_reset(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, session_id, auto_apply, suppress_parent_stage_transition
        return _complete_run(run_manager, run_id)

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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd400a",
            reset_output=True,
        )

    assert result.run_id == "dddddddd-dddd-4ddd-8ddd-dddddddd400a"
    reset.assert_called_once()


def test_synchronous_terminal_emits_event(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, session_id, auto_apply, suppress_parent_stage_transition
        return _complete_run(run_manager, run_id)

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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4001",
            auto_apply=True,
        )

    assert result.status == "completed"
    registry.emit.assert_any_call(
        "expansion_run_completed", task_id=task.id, run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4001"
    )


def test_stage_pipeline_mutex_suppresses_expansion_terminal_event(
    temp_db,
    sample_project,
) -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Pipeline expansion",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.update_task(task.id, isolation="worktree", allow_automation=True)
    initialize_manifest(
        temp_db,
        task.id,
        [
            spec("planning", 0),
            spec("expansion", 1),
            spec("development", 2),
            spec("epic_qa", 3),
            spec("merge", 4),
        ],
    )
    set_stage_state(temp_db, task.id, "planning", "done")
    set_stage_state(temp_db, task.id, "expansion", "in_progress")
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="stage-pipeline:expansion",
        ttl_seconds=300,
        run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee400c",
    )
    run_manager = LocalExpansionRunManager(temp_db)
    registry = MagicMock()
    captured: dict[str, object] = {}

    async def complete_run(
        self: ExpansionService,
        run_id: str,
        *,
        session_id: str | None,
        auto_apply: bool = True,
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, session_id, auto_apply
        captured["suppress_parent_stage_transition"] = suppress_parent_stage_transition
        return _complete_run(run_manager, run_id)

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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd400e",
            auto_apply=True,
        )

    row = task_manager.stage_states.get(task.id, "expansion")
    assert result.status == "completed"
    assert captured["suppress_parent_stage_transition"] is True
    assert row is not None
    assert row.state == "in_progress"
    registry.emit.assert_not_called()
    registry.notify.assert_called_once()


@pytest.mark.asyncio
async def test_async_start_returns_running_and_emits_later(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
        suppress_parent_stage_transition: bool = False,
    ):
        _ = self, session_id, auto_apply, suppress_parent_stage_transition
        await drain_asyncio_tasks()
        return _complete_run(run_manager, run_id)

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
            run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4008",
        )
        await drain_asyncio_tasks(cycles=2)

    assert result.status == "running"
    registry.emit.assert_any_call(
        "expansion_run_completed", task_id=task.id, run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4008"
    )
