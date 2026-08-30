"""Dispatcher heartbeat scanner tests."""

import asyncio
import inspect
import logging
import subprocess
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, Protocol, cast
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.dispatch import rules as dispatch_rules
from gobby.dispatch.actions import (
    AppendAuditMarkerAction,
    CreateIsolationAction,
    MergeWorkspaceAction,
    SpawnAgentAction,
    StartPipelineAction,
    StartStageAction,
)
from gobby.dispatch.audit import append_audit_marker
from gobby.dispatch.mutex import RuntimeDispatchMutex
from gobby.dispatch.spawn_errors import DispatchSpawnFailed
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._updates import update_task
from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

pytestmark = pytest.mark.unit

SESSION_1 = "11111111-1111-4111-8111-111111111111"
OWNER_SESSION_ID = "22222222-2222-4222-8222-222222222222"
EPIC_COMMENT_ID = "33333333-3333-4333-8333-333333333333"
UNKNOWN_TASK_ID = "44444444-4444-4444-8444-444444444444"


_LEGACY_STAGE_MAP = {
    "expanding": "expansion",
    "epic_review": "epic_qa",
    "in_development": "development",
    "merged": "merge",
}
STABLE_TEST_UUID_NAMESPACE = uuid.UUID("283ea5ca-a422-500e-b771-0533679ebc0a")


class _HasId(Protocol):
    id: str


class _HasRepoPath(Protocol):
    repo_path: str


class _DispatchInputsLogRecord(Protocol):
    registry_entry: dict[str, str]
    raw_dispatch_inputs_json: str
    error: str


class _DispatchContext(Protocol):
    stage_registry: dict[str, Any]
    agents: dict[str, Any]
    agent_definitions: dict[str, Any]


def _required[T](value: T | None) -> T:
    assert value is not None
    return value


def _field(obj: object | None, key: str, default: object | None = None) -> object | None:
    return getattr(obj, key, default)


@pytest.fixture(autouse=True)
def _sync_bundled_skills_with_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bundled agent test fixtures paired with their required skills."""
    from gobby.agents import sync as agent_sync
    from gobby.skills.sync import sync_bundled_skills

    sync_agents = agent_sync.sync_bundled_agents

    def sync_agents_and_skills(db: HubDatabase) -> dict[str, Any]:
        skill_result = sync_bundled_skills(db)
        assert skill_result["success"] is True
        return sync_agents(db)

    monkeypatch.setattr(agent_sync, "sync_bundled_agents", sync_agents_and_skills)


def stable_test_uuid(label: str) -> str:
    return str(uuid.uuid5(STABLE_TEST_UUID_NAMESPACE, f"gobby:test:{label}"))


def _task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    title: str = "Dispatch task",
    **fields: Any,
) -> Task:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title=title,
        validation_criteria="Test task completion is observable.",
    )
    legacy_lifecycle = fields.pop("lifecycle", None)
    legacy_status = fields.pop("status", None)
    stage_name = fields.pop(
        "stage_name",
        _LEGACY_STAGE_MAP.get(str(legacy_lifecycle), "development"),
    )
    stage_state = fields.pop("stage_state", "ready")
    update_task(
        temp_db,
        task.id,
        allow_automation=fields.pop("allow_automation", True),
        task_type=fields.pop("task_type", "task"),
        assigned_agent=fields.pop("assigned_agent", "backend-developer"),
        isolation=fields.pop("isolation", "none"),
        claimed_by_session_id=fields.pop("claimed_by_session_id", None),
        **fields,
    )
    initialize_manifest(temp_db, task.id, [spec(stage_name, 0)])
    set_stage_state(temp_db, task.id, stage_name, stage_state)
    if legacy_status == "closed":
        temp_db.execute(
            "UPDATE tasks SET closed_at = %s, closed_reason = %s WHERE id = %s",
            (datetime.now(UTC).isoformat(), "test_terminal", task.id),
        )
    return get_task(temp_db, task.id)


def _parent_with_stage_order(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    expansion_state: str,
    development_state: str = "ready",
) -> Task:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title=f"Parent {expansion_state}",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    update_task(
        temp_db,
        parent.id,
        allow_automation=False,
        task_type="epic",
        isolation="none",
    )
    initialize_manifest(
        temp_db,
        parent.id,
        [spec("planning", 0), spec("expansion", 1), spec("development", 2)],
    )
    set_stage_state(temp_db, parent.id, "planning", "done")
    set_stage_state(temp_db, parent.id, "expansion", expansion_state)
    set_stage_state(temp_db, parent.id, "development", development_state)
    return get_task(temp_db, parent.id)


def _mutex_storage(temp_db: HubDatabase) -> TaskDispatchMutexManager:
    return TaskDispatchMutexManager(temp_db)


def _session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_id: str = SESSION_1,
) -> str:
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            session_id,
            session_id,
            "21000000-0000-4000-8000-000000000001",
            "test",
            sample_project["id"],
        ),
    )
    return session_id


def _audit_action(task_id: str) -> AppendAuditMarkerAction:
    return AppendAuditMarkerAction(task_id=task_id, heading="Dispatch", body="marker")


@pytest.mark.asyncio
async def test_sweep_expired_leases_pages_all_active_runs(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Expired mutexes for active runs past the first page are retained."""
    from gobby.dispatch.constants import DISPATCH_HOLDER
    from gobby.dispatch.lease_cleanup import sweep_expired_leases
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    storage = _mutex_storage(temp_db)
    run_storage = LocalAgentRunManager(temp_db)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    target_run_id = ""

    for index in range(1001):
        run = run_storage.create(
            parent_session_id=system_session_id(),
            provider="codex",
            prompt=f"active run {index}",
            run_id=stable_test_uuid(f"run-active-{index:04d}"),
        )
        run_storage.start(run.id)
        started_at = (base_time + timedelta(seconds=index)).isoformat()
        temp_db.execute(
            "UPDATE agent_runs SET started_at = %s, updated_at = %s WHERE id = %s",
            (started_at, started_at, run.id),
        )
        if index == 1000:
            target_run_id = run.id

    active_task = _task(temp_db, sample_project, title="Active mutex")
    stale_task = _task(temp_db, sample_project, title="Stale mutex")
    expired_start = datetime.now(UTC) - timedelta(minutes=10)
    assert storage.acquire_mutex(
        active_task.id,
        holder=DISPATCH_HOLDER,
        kind="spawn",
        ttl_seconds=1,
        run_id=target_run_id,
        now=expired_start,
    )
    assert storage.acquire_mutex(
        stale_task.id,
        holder=DISPATCH_HOLDER,
        kind="spawn",
        ttl_seconds=1,
        run_id="dc336769-c9eb-5393-8e30-4a36c3538adb",
        now=expired_start,
    )

    assert await sweep_expired_leases(storage) == 1
    assert storage.get_mutex(active_task.id) is not None
    assert storage.get_mutex(stale_task.id) is None


async def test_sweep_expired_leases_retains_run_attached_after_candidate_select(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch.constants import DISPATCH_HOLDER
    from gobby.dispatch.lease_cleanup import sweep_expired_leases
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    storage = _mutex_storage(temp_db)
    task = _task(temp_db, sample_project, title="Raced mutex")
    expired_start = datetime.now(UTC) - timedelta(minutes=10)
    assert storage.acquire_mutex(
        task.id,
        holder=DISPATCH_HOLDER,
        kind="spawn",
        ttl_seconds=1,
        now=expired_start,
    )
    run_storage = LocalAgentRunManager(temp_db)
    run = run_storage.create(
        parent_session_id=system_session_id(),
        provider="codex",
        prompt="new active owner",
        run_id=stable_test_uuid("raced-active-run"),
    )
    run_storage.start(run.id)
    original_fetchall = temp_db.fetchall

    def fetch_candidates_then_attach_run(
        query: str,
        params: tuple[object, ...] = (),
    ) -> list[dict[str, Any]]:
        rows = original_fetchall(query, params)
        temp_db.execute(
            "UPDATE task_dispatch_mutex SET run_id = %s WHERE task_id = %s",
            (run.id, task.id),
        )
        return [dict(row) for row in rows]

    monkeypatch.setattr(temp_db, "fetchall", fetch_candidates_then_attach_run)

    assert await sweep_expired_leases(storage) == 0
    assert _required(storage.get_mutex(task.id)).run_id == run.id


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("not-a-date", None),
        ("2026-01-01T12:00:00Z", datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        ("2026-01-01T12:00:00", datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        ("2026-01-01T12:00:00+02:30", datetime(2026, 1, 1, 9, 30, tzinfo=UTC)),
    ],
)
def test_parse_mutex_timestamp(value: object, expected: datetime | None) -> None:
    from gobby.dispatch.lease_cleanup import _parse_mutex_timestamp

    assert _parse_mutex_timestamp(value) == expected


@pytest.mark.asyncio
async def test_append_audit_marker_is_exact_marker_idempotent(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = _task(temp_db, sample_project)

    assert await append_audit_marker(temp_db, task.id, "Dispatch", "marker") is True
    assert await append_audit_marker(temp_db, task.id, "Dispatch", "marker") is False
    assert await append_audit_marker(temp_db, task.id, "Dispatch", "other") is True
    description = get_task(temp_db, task.id).description or ""
    assert description.count("### Dispatch\n\nmarker") == 1
    assert description.count("### Dispatch") == 2


@pytest.mark.asyncio
async def test_append_audit_marker_only_dedupes_trailing_marker(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch.audit import audit_marker_text

    task = _task(temp_db, sample_project)
    marker = audit_marker_text("Dispatch", "marker")
    update_task(temp_db, task.id, description=f"Earlier note{marker}\n\nLater note")

    assert await append_audit_marker(temp_db, task.id, "Dispatch", "marker") is True
    description = get_task(temp_db, task.id).description or ""
    assert description.count(marker) == 2


@pytest.mark.asyncio
async def test_append_audit_marker_returns_false_on_db_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken_db = MagicMock()
    broken_db.fetchone.side_effect = psycopg.OperationalError("database unavailable")
    caplog.set_level(logging.WARNING, logger="gobby.dispatch.audit")

    assert await append_audit_marker(broken_db, UNKNOWN_TASK_ID, "Dispatch", "marker") is False
    assert f"Failed to append dispatch audit marker for task {UNKNOWN_TASK_ID}" in caplog.text


def test_development_prompt_includes_persisted_epic_failure_context(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Development prompt includes persisted epic failure context."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import rules
    from gobby.dispatch.dispatcher import build_context

    sync_bundled_agents(temp_db)
    task = _task(
        temp_db,
        sample_project,
        title="Reopened leaf",
        stage_state="in_progress",
        assigned_agent="backend-developer",
    )
    temp_db.execute(
        """
        INSERT INTO task_comments (
            id, task_id, parent_comment_id, author, author_type, body, created_at, updated_at
        )
        VALUES (
            %s, %s, NULL, 'epic-reviewer', 'system',
            '## Epic QA Follow-Up\n\nFix the dialect parity suite.', CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        )
        """,
        (EPIC_COMMENT_ID, task.id),
    )

    action = rules.development_rule(task, build_context(temp_db, task))

    assert isinstance(action, SpawnAgentAction)
    assert "Previous failure context for this follow-up work" in action.prompt
    assert "Fix the dialect parity suite." in action.prompt


class _FakePipeline:
    name = "02e3e743-e572-51b3-a0f4-83e68271282f"
    enabled = True
    deprecated = False
    steps: list[object] = []

    def model_dump_json(self) -> str:
        return '{"name":"02e3e743-e572-51b3-a0f4-83e68271282f"}'


class _FakePipelineLoader:
    def __init__(self) -> None:
        self.project_ids: list[str] = []

    async def load_pipeline(self, name: str, project_id: str) -> _FakePipeline | None:
        self.project_ids.append(project_id)
        return _FakePipeline() if name == "02e3e743-e572-51b3-a0f4-83e68271282f" else None


class _FakePipelineExecutor:
    def __init__(self) -> None:
        self.loader = _FakePipelineLoader()
        self.calls: list[dict[str, object]] = []
        self.called = asyncio.Event()

    async def execute(self, **kwargs: Any) -> SimpleNamespace:
        self.record_call(kwargs)
        return SimpleNamespace(id=kwargs["execution_id"], status="completed")

    def record_call(self, call: dict[str, object]) -> None:
        self.calls.append(call)
        self.called.set()


class _ValueErrorPipelineLoader:
    async def load_pipeline(self, _name: str, _project_id: str) -> NoReturn:
        raise ValueError("bad pipeline")


class _RuntimeErrorPipelineLoader:
    async def load_pipeline(self, _name: str, _project_id: str) -> NoReturn:
        raise RuntimeError("loader unavailable")


class _DisabledPipelineLoader:
    async def load_pipeline(self, _name: str, _project_id: str) -> SimpleNamespace:
        return SimpleNamespace(enabled=False, deprecated=False)


async def _wait_for_executor_calls(
    executor: _FakePipelineExecutor,
) -> list[dict[str, object]]:
    await asyncio.wait_for(executor.called.wait(), timeout=1)
    return executor.calls


def _pipeline_action(task_id: str) -> StartPipelineAction:
    return StartPipelineAction(
        task_id=task_id,
        task_ref="#1",
        stage_name="expansion",
        pipeline_name="02e3e743-e572-51b3-a0f4-83e68271282f",
        dispatch_inputs={"task_id": "${{ task_id }}"},
    )


@pytest.mark.asyncio
async def test_stage_pipeline_loader_value_error_escalates() -> None:
    from gobby.dispatch.stage_pipeline import start_pipeline_action

    mutex = object()
    db = object()
    escalations: list[tuple[str, bool, bool, str]] = []

    def escalate(
        action: StartPipelineAction,
        received_mutex: RuntimeDispatchMutex,
        received_db: HubDatabase,
        reason: str,
    ) -> dict[str, object]:
        escalations.append(
            (
                action.task_id,
                received_mutex is mutex,
                received_db is db,
                reason,
            )
        )
        return {"success": False, "reason": reason}

    def unexpected_sync_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("pipeline dispatch should have escalated before this call")

    async def unexpected_async_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pipeline dispatch should have escalated before this call")

    result = await start_pipeline_action(
        _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83"),
        mutex=cast(RuntimeDispatchMutex, mutex),
        db=cast(HubDatabase, db),
        context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
        services=SimpleNamespace(
            pipeline_executor=SimpleNamespace(loader=_ValueErrorPipelineLoader())
        ),
        field=_field,
        escalate_pipeline_dispatch=escalate,
        retry_neutral_pipeline_dispatch=unexpected_sync_call,
        render_dispatch_inputs=unexpected_sync_call,
        create_stage_pipeline_execution=unexpected_sync_call,
        execute_pipeline_background=unexpected_async_call,
        register_background_task=unexpected_sync_call,
    )

    assert result == {"success": False, "reason": "pipeline_invalid:bad pipeline"}
    assert escalations == [
        ("7d34e462-6ba3-5a6c-b1c6-1584b855cb83", True, True, "pipeline_invalid:bad pipeline")
    ]


@pytest.mark.asyncio
async def test_stage_pipeline_disabled_definition_escalates() -> None:
    from gobby.dispatch.stage_pipeline import start_pipeline_action

    action = _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83")
    mutex = object()
    db = object()
    escalations: list[str] = []

    def escalate(
        _action: StartPipelineAction,
        _mutex: RuntimeDispatchMutex,
        _db: HubDatabase,
        reason: str,
    ) -> dict[str, object]:
        escalations.append(reason)
        return {"success": False, "reason": reason}

    def unexpected_sync_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("disabled pipeline dispatch should stop before execution")

    async def unexpected_async_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled pipeline dispatch should stop before execution")

    result = await start_pipeline_action(
        action,
        mutex=cast(RuntimeDispatchMutex, mutex),
        db=cast(HubDatabase, db),
        context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
        services=SimpleNamespace(
            pipeline_executor=SimpleNamespace(loader=_DisabledPipelineLoader())
        ),
        field=_field,
        escalate_pipeline_dispatch=escalate,
        retry_neutral_pipeline_dispatch=unexpected_sync_call,
        render_dispatch_inputs=unexpected_sync_call,
        create_stage_pipeline_execution=unexpected_sync_call,
        execute_pipeline_background=unexpected_async_call,
        register_background_task=unexpected_sync_call,
    )

    expected_reason = f"pipeline_disabled:{action.pipeline_name}"
    assert result == {"success": False, "reason": expected_reason}
    assert escalations == [expected_reason]


@pytest.mark.asyncio
async def test_stage_pipeline_loader_unexpected_error_propagates() -> None:
    from gobby.dispatch.stage_pipeline import start_pipeline_action

    def unexpected_sync_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("unexpected loader errors should propagate before this call")

    async def unexpected_async_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected loader errors should propagate before this call")

    with pytest.raises(RuntimeError, match="loader unavailable"):
        await start_pipeline_action(
            _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83"),
            mutex=cast(RuntimeDispatchMutex, object()),
            db=cast(HubDatabase, object()),
            context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
            services=SimpleNamespace(
                pipeline_executor=SimpleNamespace(loader=_RuntimeErrorPipelineLoader())
            ),
            field=_field,
            escalate_pipeline_dispatch=unexpected_sync_call,
            retry_neutral_pipeline_dispatch=unexpected_sync_call,
            render_dispatch_inputs=unexpected_sync_call,
            create_stage_pipeline_execution=unexpected_sync_call,
            execute_pipeline_background=unexpected_async_call,
            register_background_task=unexpected_sync_call,
        )


class _EnabledPipelineLoader:
    async def load_pipeline(self, _name: str, _project_id: str) -> SimpleNamespace:
        return SimpleNamespace(enabled=True, deprecated=False)


def _unexpected_pipeline_call(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("pipeline dispatch should not reach this call")


@pytest.mark.asyncio
async def test_stage_pipeline_spawn_runs_on_current_loop_when_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import stage_pipeline

    monkeypatch.setattr(
        stage_pipeline, "reset_stage_pipeline_retry_neutral", lambda *_a, **_k: None
    )
    executed: dict[str, object] = {}
    registered: list[tuple[str, asyncio.Task[object]]] = []

    async def fake_execute(
        _executor: object,
        _pipeline: object,
        _inputs: object,
        _project_id: object,
        execution_id: str,
        _pipeline_name: str,
        session_id: str | None = None,
    ) -> None:
        executed["loop"] = asyncio.get_running_loop()
        executed["execution_id"] = execution_id

    def register_background_task(execution_id: str, task: asyncio.Task[Any]) -> None:
        registered.append((execution_id, task))

    result = await stage_pipeline.start_pipeline_action(
        _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83"),
        mutex=cast(RuntimeDispatchMutex, object()),
        db=cast(HubDatabase, object()),
        context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
        services=SimpleNamespace(
            pipeline_executor=SimpleNamespace(loader=_EnabledPipelineLoader()),
            main_loop=asyncio.get_running_loop(),
            triggering_session_id=None,
        ),
        field=_field,
        escalate_pipeline_dispatch=_unexpected_pipeline_call,
        retry_neutral_pipeline_dispatch=_unexpected_pipeline_call,
        render_dispatch_inputs=lambda *_a, **_k: {},
        create_stage_pipeline_execution=lambda *_a, **_k: "exec-current-loop",
        execute_pipeline_background=fake_execute,
        register_background_task=register_background_task,
    )

    assert result == {"success": True, "execution_id": "exec-current-loop", "status": "running"}
    assert registered and registered[0][0] == "exec-current-loop"
    await registered[0][1]
    assert executed["loop"] is asyncio.get_running_loop()
    assert executed["execution_id"] == "exec-current-loop"


@pytest.mark.asyncio
async def test_stage_pipeline_spawn_hands_off_to_main_loop_from_ephemeral_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick on a short-lived loop must not strand the execution (task #18611).

    Mirrors the HTTP build route, which drives the build service via
    asyncio.run inside a worker thread: the pipeline coroutine must land on
    the durable main loop and run to completion after the tick's loop is gone.
    """
    from gobby.dispatch import stage_pipeline

    monkeypatch.setattr(
        stage_pipeline, "reset_stage_pipeline_retry_neutral", lambda *_a, **_k: None
    )
    main_loop = asyncio.get_running_loop()
    executed: dict[str, object] = {}
    execution_done = asyncio.Event()
    registered: list[tuple[str, asyncio.Task[object]]] = []

    async def fake_execute(
        _executor: object,
        _pipeline: object,
        _inputs: object,
        _project_id: object,
        execution_id: str,
        _pipeline_name: str,
        session_id: str | None = None,
    ) -> None:
        executed["loop"] = asyncio.get_running_loop()
        executed["execution_id"] = execution_id
        execution_done.set()

    def register_background_task(execution_id: str, task: asyncio.Task[Any]) -> None:
        registered.append((execution_id, task))

    services = SimpleNamespace(
        pipeline_executor=SimpleNamespace(loader=_EnabledPipelineLoader()),
        main_loop=main_loop,
        triggering_session_id=None,
    )

    def run_tick_on_ephemeral_loop() -> dict[str, object]:
        return asyncio.run(
            stage_pipeline.start_pipeline_action(
                _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83"),
                mutex=cast(RuntimeDispatchMutex, object()),
                db=cast(HubDatabase, object()),
                context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
                services=services,
                field=_field,
                escalate_pipeline_dispatch=_unexpected_pipeline_call,
                retry_neutral_pipeline_dispatch=_unexpected_pipeline_call,
                render_dispatch_inputs=lambda *_a, **_k: {},
                create_stage_pipeline_execution=lambda *_a, **_k: "exec-handoff",
                execute_pipeline_background=fake_execute,
                register_background_task=register_background_task,
            )
        )

    result = await asyncio.to_thread(run_tick_on_ephemeral_loop)

    assert result == {"success": True, "execution_id": "exec-handoff", "status": "running"}
    await asyncio.wait_for(execution_done.wait(), timeout=2)
    assert executed["loop"] is main_loop
    assert executed["execution_id"] == "exec-handoff"
    assert registered and registered[0][0] == "exec-handoff"
    assert registered[0][1].get_loop() is main_loop
    await registered[0][1]


@pytest.mark.asyncio
async def test_stage_pipeline_closes_coroutine_when_target_loop_task_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import stage_pipeline

    monkeypatch.setattr(
        stage_pipeline, "reset_stage_pipeline_retry_neutral", lambda *_a, **_k: None
    )
    execution_manager = MagicMock()
    created_coroutines: list[Coroutine[Any, Any, None]] = []
    created_task_names: list[str | None] = []
    action = _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83")

    async def fake_execute() -> None:
        raise AssertionError("pipeline with failed task creation must not execute")

    def capture_execute(
        _executor: object,
        _pipeline: object,
        _inputs: object,
        _project_id: str,
        _execution_id: str,
        _pipeline_name: str,
        *,
        session_id: str | None = None,
    ) -> Coroutine[Any, Any, None]:
        coroutine = fake_execute()
        created_coroutines.append(coroutine)
        return coroutine

    class ImmediateLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback: Callable[[], object]) -> None:
            callback()

    def fail_create_task(
        _coroutine: Coroutine[Any, Any, object],
        *,
        name: str | None = None,
    ) -> NoReturn:
        created_task_names.append(name)
        raise RuntimeError("task creation failed")

    monkeypatch.setattr(asyncio, "create_task", fail_create_task)

    result = await stage_pipeline.start_pipeline_action(
        action,
        mutex=cast(RuntimeDispatchMutex, object()),
        db=cast(HubDatabase, object()),
        context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
        services=SimpleNamespace(
            pipeline_executor=SimpleNamespace(
                loader=_EnabledPipelineLoader(),
                execution_manager=execution_manager,
            ),
            main_loop=ImmediateLoop(),
            triggering_session_id=None,
        ),
        field=_field,
        escalate_pipeline_dispatch=lambda _action, _mutex, _db, reason: {
            "success": False,
            "error": reason,
        },
        retry_neutral_pipeline_dispatch=_unexpected_pipeline_call,
        render_dispatch_inputs=lambda *_a, **_k: {},
        create_stage_pipeline_execution=lambda *_a, **_k: "exec-create-task-failure",
        execute_pipeline_background=capture_execute,
        register_background_task=lambda *_a, **_k: None,
    )

    assert result == {
        "success": False,
        "error": "pipeline_start_registration_failed:task creation failed",
    }
    assert created_task_names == [f"stage-pipeline-{action.pipeline_name}-exec-cre"]
    assert len(created_coroutines) == 1
    assert inspect.getcoroutinestate(created_coroutines[0]) == inspect.CORO_CLOSED
    execution_manager.update_execution_status.assert_called_once()


@pytest.mark.asyncio
async def test_stage_pipeline_spawn_fails_when_target_loop_does_not_acknowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import stage_pipeline

    monkeypatch.setattr(
        stage_pipeline, "reset_stage_pipeline_retry_neutral", lambda *_a, **_k: None
    )
    monkeypatch.setattr(stage_pipeline, "PIPELINE_START_ACK_TIMEOUT_SECONDS", 0.01)
    target_loop = asyncio.new_event_loop()
    loop_started = threading.Event()
    registration_started = threading.Event()
    release_registration = threading.Event()
    task_cancelled = threading.Event()
    executed = threading.Event()
    spawned_tasks: list[asyncio.Task[Any]] = []
    wait_call_count = 0
    real_wait_for = asyncio.wait_for

    def run_target_loop() -> None:
        asyncio.set_event_loop(target_loop)
        loop_started.set()
        target_loop.run_forever()

    target_thread = threading.Thread(target=run_target_loop, daemon=True)
    target_thread.start()
    assert loop_started.wait(timeout=2)

    async def controlled_wait_for(awaitable: Awaitable[Any], timeout: float | None) -> Any:
        nonlocal wait_call_count
        wait_call_count += 1
        if wait_call_count == 1:
            assert await asyncio.to_thread(registration_started.wait, 2)
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise TimeoutError
        release_registration.set()
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", controlled_wait_for)

    execution_manager = MagicMock()
    services = SimpleNamespace(
        pipeline_executor=SimpleNamespace(
            loader=_EnabledPipelineLoader(),
            execution_manager=execution_manager,
        ),
        main_loop=target_loop,
        triggering_session_id=None,
    )
    escalated: list[str] = []

    async def fake_execute(*_args: object, **_kwargs: object) -> None:
        executed.set()

    def register_background_task(_execution_id: str, task: asyncio.Task[Any]) -> None:
        spawned_tasks.append(task)
        task.add_done_callback(lambda done: task_cancelled.set() if done.cancelled() else None)
        registration_started.set()
        assert release_registration.wait(timeout=2)

    def record_escalation(
        _action: StartPipelineAction,
        _mutex: RuntimeDispatchMutex,
        _db: HubDatabase,
        reason: str,
    ) -> dict[str, object]:
        assert spawned_tasks and spawned_tasks[0].cancelling()
        escalated.append(reason)
        return {"success": False, "error": reason}

    try:
        result = await stage_pipeline.start_pipeline_action(
            _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83"),
            mutex=cast(RuntimeDispatchMutex, object()),
            db=cast(HubDatabase, object()),
            context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
            services=services,
            field=_field,
            escalate_pipeline_dispatch=record_escalation,
            retry_neutral_pipeline_dispatch=_unexpected_pipeline_call,
            render_dispatch_inputs=lambda *_a, **_k: {},
            create_stage_pipeline_execution=lambda *_a, **_k: "exec-timeout",
            execute_pipeline_background=fake_execute,
            register_background_task=register_background_task,
        )
        assert await asyncio.to_thread(task_cancelled.wait, 2)
    finally:
        release_registration.set()
        target_loop.call_soon_threadsafe(target_loop.stop)
        target_thread.join(timeout=2)
        target_loop.close()

    assert result == {
        "success": False,
        "error": "pipeline_start_registration_timeout",
    }
    assert escalated == ["pipeline_start_registration_timeout"]
    assert wait_call_count == 2
    assert executed.is_set() is False
    execution_manager.update_execution_status.assert_called_once()


@pytest.mark.asyncio
async def test_stage_pipeline_timeout_cancels_task_registered_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import stage_pipeline

    monkeypatch.setattr(
        stage_pipeline, "reset_stage_pipeline_retry_neutral", lambda *_a, **_k: None
    )
    monkeypatch.setattr(stage_pipeline, "PIPELINE_START_ACK_TIMEOUT_SECONDS", 0.01)
    target_loop = asyncio.new_event_loop()
    loop_started = threading.Event()
    registration_started = threading.Event()
    release_registration = threading.Event()
    task_cancelled = threading.Event()
    executed = threading.Event()

    def run_target_loop() -> None:
        asyncio.set_event_loop(target_loop)
        loop_started.set()
        target_loop.run_forever()

    target_thread = threading.Thread(target=run_target_loop, daemon=True)
    target_thread.start()
    assert loop_started.wait(timeout=2)

    execution_manager = MagicMock()
    services = SimpleNamespace(
        pipeline_executor=SimpleNamespace(
            loader=_EnabledPipelineLoader(),
            execution_manager=execution_manager,
        ),
        main_loop=target_loop,
        triggering_session_id=None,
    )

    async def fake_execute(*_args: object, **_kwargs: object) -> None:
        executed.set()

    def register_late(_execution_id: str, task: asyncio.Task[Any]) -> None:
        task.add_done_callback(lambda done: task_cancelled.set() if done.cancelled() else None)
        registration_started.set()
        assert release_registration.wait(timeout=2)

    try:
        result = await stage_pipeline.start_pipeline_action(
            _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83"),
            mutex=cast(RuntimeDispatchMutex, object()),
            db=cast(HubDatabase, object()),
            context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
            services=services,
            field=_field,
            escalate_pipeline_dispatch=lambda _action, _mutex, _db, reason: {
                "success": False,
                "error": reason,
            },
            retry_neutral_pipeline_dispatch=_unexpected_pipeline_call,
            render_dispatch_inputs=lambda *_a, **_k: {},
            create_stage_pipeline_execution=lambda *_a, **_k: "exec-late",
            execute_pipeline_background=fake_execute,
            register_background_task=register_late,
        )
        assert registration_started.is_set()
        release_registration.set()
        assert await asyncio.to_thread(task_cancelled.wait, 2)
    finally:
        release_registration.set()
        target_loop.call_soon_threadsafe(target_loop.stop)
        target_thread.join(timeout=2)
        target_loop.close()

    assert result == {
        "success": False,
        "error": "pipeline_start_registration_timeout",
    }
    assert executed.is_set() is False
    execution_manager.update_execution_status.assert_called_once()


@pytest.mark.asyncio
async def test_stage_pipeline_handles_loop_closing_before_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import stage_pipeline

    monkeypatch.setattr(
        stage_pipeline, "reset_stage_pipeline_retry_neutral", lambda *_a, **_k: None
    )

    class ClosingLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, _callback: object) -> None:
            raise RuntimeError("event loop is closed")

    execution_manager = MagicMock()
    services = SimpleNamespace(
        pipeline_executor=SimpleNamespace(
            loader=_EnabledPipelineLoader(),
            execution_manager=execution_manager,
        ),
        main_loop=ClosingLoop(),
        triggering_session_id=None,
    )
    created_coroutines: list[Coroutine[Any, Any, None]] = []

    async def fake_execute() -> None:
        raise AssertionError("unscheduled pipeline must not execute")

    def capture_execute(
        _executor: object,
        _pipeline: object,
        _inputs: object,
        _project_id: str,
        _execution_id: str,
        _pipeline_name: str,
        *,
        session_id: str | None = None,
    ) -> Coroutine[Any, Any, None]:
        coroutine = fake_execute()
        created_coroutines.append(coroutine)
        return coroutine

    result = await stage_pipeline.start_pipeline_action(
        _pipeline_action("7d34e462-6ba3-5a6c-b1c6-1584b855cb83"),
        mutex=cast(RuntimeDispatchMutex, object()),
        db=cast(HubDatabase, object()),
        context=SimpleNamespace(project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06"),
        services=services,
        field=_field,
        escalate_pipeline_dispatch=lambda _action, _mutex, _db, reason: {
            "success": False,
            "error": reason,
        },
        retry_neutral_pipeline_dispatch=_unexpected_pipeline_call,
        render_dispatch_inputs=lambda *_a, **_k: {},
        create_stage_pipeline_execution=lambda *_a, **_k: "exec-loop-close",
        execute_pipeline_background=capture_execute,
        register_background_task=lambda *_a, **_k: None,
    )

    assert result == {"success": False, "error": "pipeline_start_loop_closed"}
    assert len(created_coroutines) == 1
    assert inspect.getcoroutinestate(created_coroutines[0]) == inspect.CORO_CLOSED
    execution_manager.update_execution_status.assert_called_once()


def test_candidate_filter_excludes_claimed_leased_blocked_terminal(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Candidate filter excludes claimed leased blocked terminal."""
    from gobby.storage.tasks import _automation

    ready = _task(temp_db, sample_project, "ready")
    _task(
        temp_db, sample_project, "claimed", claimed_by_session_id=_session(temp_db, sample_project)
    )
    leased = _task(temp_db, sample_project, "leased")
    terminal = _task(temp_db, sample_project, "terminal", lifecycle="merged", status="closed")
    blocked = _task(temp_db, sample_project, "blocked")
    blocker = _task(temp_db, sample_project, "blocker", allow_automation=False)

    _mutex_storage(temp_db).acquire_mutex(
        leased.id,
        holder="other",
        kind="test",
        ttl_seconds=30,
    )
    temp_db.execute(
        "UPDATE tasks SET closed_at = %s WHERE id = %s",
        (datetime.now(UTC).isoformat(), terminal.id),
    )
    temp_db.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at)
        VALUES (%s, %s, 'blocks', %s)
        """,
        (blocked.id, blocker.id, datetime.now(UTC).isoformat()),
    )

    candidates = _automation.list_automation_candidates(temp_db, project_id=sample_project["id"])

    assert [candidate.id for candidate in candidates] == [ready.id]
    assert not _automation.is_blocked_by_deps(candidates[0])


@pytest.mark.asyncio
async def test_heartbeat_blocks_child_development_while_parent_expansion_needs_review(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch import dispatcher
    from gobby.storage.tasks import _automation

    parent = _parent_with_stage_order(
        temp_db,
        sample_project,
        expansion_state="needs_review",
    )
    child = _task(
        temp_db,
        sample_project,
        title="Blocked child",
        parent_task_id=parent.id,
        stage_name="development",
        stage_state="ready",
    )

    candidates = _automation.list_automation_candidates(temp_db, project_id=sample_project["id"])
    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert child.id not in {candidate.id for candidate in candidates}
    assert result.executed == 0
    assert (
        _required(LocalTaskManager(temp_db).stage_states.get(child.id, "development")).state
        == "ready"
    )


@pytest.mark.parametrize("parent_development_state", ["ready", "in_progress"])
@pytest.mark.asyncio
async def test_heartbeat_allows_child_development_after_parent_expansion_done(
    parent_development_state: str,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch import dispatcher

    parent = _parent_with_stage_order(
        temp_db,
        sample_project,
        expansion_state="done",
        development_state=parent_development_state,
    )
    child = _task(
        temp_db,
        sample_project,
        title=f"Allowed child {parent_development_state}",
        parent_task_id=parent.id,
        stage_name="development",
        stage_state="ready",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert _required(LocalTaskManager(temp_db).stage_states.get(child.id, "development")).state == (
        "in_progress"
    )


@pytest.mark.asyncio
async def test_heartbeat_records_gated_epic_root_before_reopened_descendant(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch import dispatcher

    manager = LocalTaskManager(temp_db)
    root = manager.create_task(
        project_id=sample_project["id"],
        title="Epic root",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    update_task(temp_db, root.id, allow_automation=True, isolation="none", task_type="epic")
    initialize_manifest(
        temp_db,
        root.id,
        [spec("development", 0), spec("epic_qa", 1), spec("merge", 2)],
    )
    set_stage_state(temp_db, root.id, "development", "done")
    set_stage_state(temp_db, root.id, "epic_qa", "ready")
    phase = manager.create_task(
        project_id=sample_project["id"],
        title="Integrated phase",
        parent_task_id=root.id,
        validation_criteria="Test task completion is observable.",
    )
    child = _task(
        temp_db,
        sample_project,
        title="Reopened child",
        parent_task_id=phase.id,
        stage_name="development",
        stage_state="ready",
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        max_actions=1,
    )

    assert result.executed == 1
    assert (
        _required(LocalTaskManager(temp_db).stage_states.get(child.id, "development")).state
        == "ready"
    )
    assert (
        _required(LocalTaskManager(temp_db).stage_states.get(root.id, "epic_qa")).state == "ready"
    )
    root_description = get_task(temp_db, root.id).description or ""
    assert "### Epic QA deferred" in root_description


@pytest.mark.asyncio
async def test_heartbeat_dispatches_reopened_review_under_gated_epic_root(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(
        project_id=sample_project["id"],
        title="Epic root",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    update_task(temp_db, root.id, allow_automation=True, isolation="none", task_type="epic")
    initialize_manifest(
        temp_db,
        root.id,
        [spec("development", 0), spec("epic_qa", 1), spec("merge", 2)],
    )
    set_stage_state(temp_db, root.id, "development", "done")
    set_stage_state(temp_db, root.id, "epic_qa", "ready")
    child = _task(
        temp_db,
        sample_project,
        title="Reopened child review",
        parent_task_id=root.id,
        category="code",
        stage_name="development",
        stage_state="needs_review",
    )
    temp_db.execute(
        """
        UPDATE task_stage_states
        SET reviewer_agent = %s, review_policy = %s
        WHERE task_id = %s AND stage_name = %s
        """,
        ("qa-reviewer", "required", child.id, "development"),
    )

    spawned: list[SpawnAgentAction] = []

    async def fake_spawn_agent(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append(action)
        return "626a0b1f-dfa7-5b3e-989d-30e173917443"

    monkeypatch.setattr(dispatcher, "spawn_agent", fake_spawn_agent)

    first = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        max_actions=1,
    )
    second = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        max_actions=1,
    )

    assert first.executed == 1
    assert second.executed == 1
    assert len(spawned) == 1
    assert spawned[0].task_id == child.id
    assert spawned[0].agent_slug == "qa-reviewer"


@pytest.mark.asyncio
async def test_heartbeat_escalates_exhausted_epic_qa_review(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch import dispatcher

    task = _task(
        temp_db,
        sample_project,
        stage_name="epic_qa",
        stage_state="needs_review",
    )
    temp_db.execute(
        """
        UPDATE task_stage_states
        SET review_round_count = %s, max_review_rounds = %s
        WHERE task_id = %s AND stage_name = %s
        """,
        (2, 2, task.id, "epic_qa"),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        max_actions=1,
    )

    escalated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert escalated.is_escalated is True
    assert escalated.escalation_reason == "epic_qa_max_review_rounds"


def test_count_active_agents_scopes_by_parent_session_project(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Count active agents scopes by parent session project."""
    from gobby.dispatch.dispatcher import count_active_agents
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    sessions = SessionManager(temp_db)
    agents = LocalAgentRunManager(temp_db)
    other_project = LocalProjectManager(temp_db).create(name="other-project")
    parent_a = sessions.register(
        external_id="parent-a",
        machine_id=None,
        source="test",
        project_id=sample_project["id"],
    )
    parent_b = sessions.register(
        external_id="parent-b",
        machine_id=None,
        source="test",
        project_id=other_project.id,
    )
    run_a = agents.create(parent_session_id=parent_a.id, provider="codex", prompt="a")
    run_b = agents.create(parent_session_id=parent_b.id, provider="codex", prompt="b")
    run_done = agents.create(parent_session_id=parent_a.id, provider="codex", prompt="done")
    agents.start(run_a.id)
    agents.start(run_b.id)
    agents.complete(run_done.id, result="done")

    assert count_active_agents(temp_db) == 2
    assert count_active_agents(temp_db, project_id=sample_project["id"]) == 1
    assert count_active_agents(temp_db, project_id=other_project.id) == 1


@pytest.mark.asyncio
async def test_max_active_agents_cap(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Max active agents cap."""
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    spawned: list[object] = []

    def record_spawn(*args: object, **_kwargs: object) -> None:
        spawned.append(args)

    monkeypatch.setattr(dispatcher, "count_active_agents", lambda *args, **kwargs: 2)
    monkeypatch.setattr(dispatcher, "MAX_ACTIVE_AGENTS", 2)
    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.cap_reached is True
    assert spawned == []


@pytest.mark.asyncio
async def test_heartbeat_reaps_stale_pending_runs_before_agent_cap(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    calls: list[str] = []

    class FakeLifecycleMonitor:
        async def run_acknowledged_stale_sweeps(
            self,
            *,
            pending_timeout_minutes: int,
        ) -> list[str]:
            assert pending_timeout_minutes == 60
            calls.append("cleanup")
            return ["run-stale"]

    monkeypatch.setattr(dispatcher, "count_active_agents", lambda *args, **kwargs: 2)
    monkeypatch.setattr(dispatcher, "MAX_ACTIVE_AGENTS", 2)

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=SimpleNamespace(agent_lifecycle_monitor=FakeLifecycleMonitor()),
    )

    assert result.cap_reached is True
    assert calls == ["cleanup"]


@pytest.mark.asyncio
async def test_concurrent_project_heartbeats_share_global_agent_cap(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Concurrent project heartbeats cannot spawn past the global active-agent cap."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    first_task = _task(temp_db, sample_project, title="First dispatch", stage_state="in_progress")
    other_project = LocalProjectManager(temp_db).create(name="other-dispatch-project")
    other_project_dict = {"id": other_project.id}
    second_task = _task(
        temp_db,
        other_project_dict,
        title="Second dispatch",
        stage_state="in_progress",
    )
    sessions = SessionManager(temp_db)
    first_parent_session = sessions.register(
        external_id="dispatcher-parent",
        machine_id=None,
        source="test",
        project_id=sample_project["id"],
    )
    second_parent_session = sessions.register(
        external_id="other-dispatcher-parent",
        machine_id=None,
        source="test",
        project_id=other_project.id,
    )
    agents = LocalAgentRunManager(temp_db)
    parent_sessions = {
        first_task.id: first_parent_session.id,
        second_task.id: second_parent_session.id,
    }

    first_spawn_admitted = asyncio.Event()
    second_spawn_admitted = asyncio.Event()
    release_first_spawn = asyncio.Event()
    spawned_task_ids: list[str] = []
    original_count_active_agents = dispatcher.count_active_agents

    async def fake_spawn_agent(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned_task_ids.append(action.task_id)
        is_first_spawn = len(spawned_task_ids) == 1
        if is_first_spawn:
            first_spawn_admitted.set()
            await release_first_spawn.wait()
        else:
            second_spawn_admitted.set()
        run = agents.create(
            parent_session_id=parent_sessions[action.task_id],
            provider="codex",
            prompt=action.prompt,
            task_id=action.task_id,
        )
        return run.id

    async def run_dispatch(project_id: str) -> dispatcher.HeartbeatResult:
        return await dispatcher._run_heartbeat_unlocked(
            db=temp_db,
            project_id=project_id,
            max_active_agents=1,
            max_actions=1,
        )

    monkeypatch.setattr(dispatcher, "spawn_agent", fake_spawn_agent)

    first = asyncio.create_task(run_dispatch(sample_project["id"]))
    second: asyncio.Task[dispatcher.HeartbeatResult] | None = None
    try:
        await asyncio.wait_for(first_spawn_admitted.wait(), 5)
        second = asyncio.create_task(run_dispatch(other_project.id))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_spawn_admitted.wait(), 0.1)
        release_first_spawn.set()
        first_result, second_result = await asyncio.gather(first, second)
    finally:
        release_first_spawn.set()
        pending = [task for task in (first, second) if task is not None and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert first_result.executed == 1
    assert second_result.executed == 0
    assert second_result.cap_reached is True
    assert spawned_task_ids == [first_task.id]
    assert original_count_active_agents(temp_db) == 1
    assert original_count_active_agents(temp_db, project_id=sample_project["id"]) == 1
    assert original_count_active_agents(temp_db, project_id=other_project.id) == 0
    assert second_task.id not in spawned_task_ids


@pytest.mark.parametrize("first_outcome", ["failure", "cancelled"])
@pytest.mark.asyncio
async def test_global_agent_cap_admission_releases_after_interrupted_spawn(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    first_outcome: str,
) -> None:
    """Failed and cancelled admissions release capacity for the next project."""
    from gobby.dispatch import dispatcher
    from gobby.dispatch.actions import SpawnAgentAction
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    other_project = LocalProjectManager(temp_db).create(name=f"cap-recovery-{first_outcome}")
    sessions = SessionManager(temp_db)
    parent_sessions = {
        sample_project["id"]: sessions.register(
            external_id=f"cap-first-{first_outcome}",
            machine_id=None,
            source="test",
            project_id=sample_project["id"],
        ).id,
        other_project.id: sessions.register(
            external_id=f"cap-second-{first_outcome}",
            machine_id=None,
            source="test",
            project_id=other_project.id,
        ).id,
    }
    actions = {
        project_id: SpawnAgentAction(
            task_id=f"task-{project_id}",
            task_ref=f"task-{project_id}",
            agent_slug="backend-developer",
            prompt="test",
        )
        for project_id in parent_sessions
    }
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    runs = LocalAgentRunManager(temp_db)

    async def fake_execute_action(
        action: SpawnAgentAction,
        **_kwargs: object,
    ) -> str:
        project_id = action.task_id.removeprefix("task-")
        if project_id == sample_project["id"]:
            first_entered.set()
            await release_first.wait()
            raise DispatchSpawnFailed("injected spawn failure")
        second_entered.set()
        return runs.create(
            parent_session_id=parent_sessions[project_id],
            provider="codex",
            prompt=action.prompt,
            task_id=None,
        ).id

    async def fake_handle_spawn_failure(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(dispatcher, "_execute_action", fake_execute_action)
    monkeypatch.setattr(dispatcher, "_handle_spawn_failure", fake_handle_spawn_failure)

    async def admit(project_id: str, mutex: MagicMock) -> object | None:
        return await dispatcher._execute_action_with_agent_cap(
            actions[project_id],
            mutex=mutex,
            db=temp_db,
            context=object(),
            services=None,
            project_id=project_id,
            cap=1,
        )

    first = asyncio.create_task(admit(sample_project["id"], MagicMock()))
    second: asyncio.Task[object | None] | None = None
    try:
        await asyncio.wait_for(first_entered.wait(), 5)
        second = asyncio.create_task(admit(other_project.id, MagicMock()))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_entered.wait(), 0.1)

        if first_outcome == "cancelled":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release_first.set()
            assert await first is None

        second_run_id = await asyncio.wait_for(second, 5)
    finally:
        release_first.set()
        pending = [task for task in (first, second) if task is not None and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert isinstance(second_run_id, str)
    assert runs.get(second_run_id) is not None
    assert dispatcher.count_active_agents(temp_db) == 1


@pytest.mark.asyncio
async def test_run_heartbeat_serializes_overlapping_development_start_actions(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Run heartbeat serializes overlapping development start actions."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "shared refactor", priority=1)
    second = _task(temp_db, sample_project, "shared follow-up", priority=2)
    af_manager = TaskAffectedFileManager(temp_db)
    af_manager.set_files(first.id, ["src/gobby/config/bootstrap.py"], source="expansion")
    af_manager.set_files(second.id, ["src/gobby/config/bootstrap.py"], source="expansion")

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    stage_states = LocalTaskManager(temp_db).stage_states
    assert result.executed == 1
    assert result.skipped == 1
    assert _required(stage_states.get(first.id, "development")).state == "in_progress"
    assert _required(stage_states.get(second.id, "development")).state == "ready"


@pytest.mark.asyncio
async def test_run_heartbeat_allows_disjoint_development_write_sets(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Run heartbeat allows disjoint development write sets."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "config refactor", priority=1)
    second = _task(temp_db, sample_project, "routes follow-up", priority=2)
    af_manager = TaskAffectedFileManager(temp_db)
    af_manager.set_files(first.id, ["src/gobby/config/bootstrap.py"], source="expansion")
    af_manager.set_files(second.id, ["src/gobby/servers/routes/build.py"], source="expansion")

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    stage_states = LocalTaskManager(temp_db).stage_states
    assert result.executed == 2
    assert result.skipped == 0
    assert _required(stage_states.get(first.id, "development")).state == "in_progress"
    assert _required(stage_states.get(second.id, "development")).state == "in_progress"


@pytest.mark.asyncio
async def test_run_heartbeat_max_actions_stops_after_one_lifecycle_action(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A quick dispatcher pass runs exactly one action even with multiple ready tasks."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "first quick action")
    second = _task(temp_db, sample_project, "second quick action")

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        max_actions=1,
    )

    stage_states = LocalTaskManager(temp_db).stage_states
    assert result.executed == 1
    assert result.cap_reached is True
    assert _required(stage_states.get(first.id, "development")).state == "in_progress"
    assert _required(stage_states.get(second.id, "development")).state == "ready"


@pytest.mark.asyncio
async def test_run_heartbeat_blocks_ready_task_behind_active_overlapping_write_set(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run heartbeat blocks ready task behind active overlapping write set."""
    from gobby.dispatch import dispatcher

    owner_session_id = _session(temp_db, sample_project, OWNER_SESSION_ID)
    active = _task(
        temp_db,
        sample_project,
        "active config work",
        stage_state="in_progress",
        claimed_by_session_id=owner_session_id,
    )
    waiting = _task(temp_db, sample_project, "waiting config work")
    af_manager = TaskAffectedFileManager(temp_db)
    af_manager.set_files(active.id, ["src/gobby/config/bootstrap.py"], source="expansion")
    af_manager.set_files(waiting.id, ["src/gobby/config/bootstrap.py"], source="expansion")
    metric_outcomes: list[str] = []

    def record_metric(component: str, outcome: str) -> None:
        metric_outcomes.append(f"{component}:{outcome}")

    monkeypatch.setattr(
        dispatcher,
        "record_automation_event",
        record_metric,
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 0
    assert result.skipped == 1
    assert metric_outcomes == ["dispatcher:skipped"]
    assert (
        _required(LocalTaskManager(temp_db).stage_states.get(waiting.id, "development")).state
        == "ready"
    )


@pytest.mark.asyncio
async def test_run_heartbeat_skips_spawn_when_daemon_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Run heartbeat skips spawn when daemon not ready."""
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    spawned: list[object] = []
    services = SimpleNamespace(startup_ready=False, shutdown_in_progress=False)

    def record_spawn(*args: object, **_kwargs: object) -> None:
        spawned.append(args)

    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.reason == "daemon_startup_not_ready"
    assert result.executed == 0
    assert spawned == []


@pytest.mark.asyncio
async def test_cancelled_spawn_releases_no_run_mutex(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Cancelled spawn releases no run mutex."""
    from gobby.dispatch import dispatcher
    from gobby.dispatch.mutex import RuntimeDispatchMutex

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    mutex = RuntimeDispatchMutex(
        storage,
        task_id=task.id,
        holder="dispatcher",
        action_kind="heartbeat",
        ttl_seconds=600,
    )
    mutex.__enter__()

    async def cancelled_spawn(*_args: object, **_kwargs: object) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(dispatcher, "spawn_agent", cancelled_spawn)

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.execute_action(
            SpawnAgentAction(
                task_id=task.id,
                task_ref=f"#{task.seq_num}",
                agent_slug="backend-developer",
                prompt="do work",
            ),
            mutex=mutex,
            db=temp_db,
        )

    assert storage.get_mutex(task.id) is None


async def test_cancelled_heartbeat_candidate_releases_mutex_before_next_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Heartbeat cancellation cannot strand a candidate mutex."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = _audit_action(task.id)
    first_attempt_started = asyncio.Event()
    attempts = 0

    async def cancel_first_action(*_args: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            await asyncio.Future()
        return True

    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(dispatcher, "execute_action", cancel_first_action)

    heartbeat = asyncio.create_task(
        dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    )
    await first_attempt_started.wait()
    heartbeat.cancel()

    with pytest.raises(asyncio.CancelledError):
        await heartbeat

    assert storage.get_mutex(task.id) is None

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert attempts == 2
    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_mutex_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Mutex lifecycle."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _audit_action(task.id),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id) is None
    assert "### Dispatch" in (get_task(temp_db, task.id).description or "")


@pytest.mark.asyncio
async def test_toctou_skip_on_changed_tuple(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Toctou skip on changed tuple."""
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    executed: list[object] = []

    def reload_changed(task_id: str, **kwargs: Any) -> Task:
        return get_task(temp_db, task_id) if executed else _task_changed(temp_db, task_id)

    def record_action(action: object, **_kwargs: object) -> None:
        executed.append(action)

    monkeypatch.setattr(dispatcher, "reload_candidate", reload_changed)
    monkeypatch.setattr(dispatcher, "execute_action", record_action)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.skipped == 1
    assert executed == []


def _task_changed(temp_db: HubDatabase, task_id: str) -> Task:
    set_stage_state(temp_db, task_id, "development", "in_progress")
    return get_task(temp_db, task_id)


@pytest.mark.asyncio
async def test_first_match_action_executed(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """First match action executed."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project)
    executed: list[object] = []
    action = _audit_action(task.id)

    def record_action(action: object, **_kwargs: object) -> None:
        executed.append(action)

    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(dispatcher, "execute_action", record_action)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert executed == [action]


@pytest.mark.asyncio
async def test_spawn_action_links_run_id(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Spawn action links run id."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref="#1",
        agent_slug="backend-developer",
        prompt="go",
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(
        dispatcher, "spawn_agent", lambda *args, **kwargs: "ac314d27-4314-5fe3-a0ab-01645086e137"
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert _required(storage.get_mutex(task.id)).run_id == "ac314d27-4314-5fe3-a0ab-01645086e137"


@pytest.mark.asyncio
async def test_spawn_action_skips_stale_candidate_with_active_run_mutex(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Stale candidates cannot overwrite an existing active run mutex."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=600,
        run_id="28fb95f3-ad0b-593f-ac6f-e084ad49d2d2",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawned: list[tuple[str, str, str]] = []

    def record_spawn(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append((action.task_id, action.task_ref, action.agent_slug))
        return "ad91abd1-f0f0-527c-a037-2270467bb189"

    monkeypatch.setattr(dispatcher, "list_automation_candidates", lambda *args, **kwargs: [task])
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 0
    assert result.skipped == 1
    assert spawned == []
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id == "28fb95f3-ad0b-593f-ac6f-e084ad49d2d2"


@pytest.mark.asyncio
async def test_spawn_attach_failure_terminalizes_created_run(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Mutex attach failure cannot leak an active spawned run."""
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")
    run_storage = LocalAgentRunManager(temp_db)
    killed: list[str] = []

    def fake_spawn_agent(*_args: object, **_kwargs: object) -> str:
        run = run_storage.create(
            parent_session_id=system_session_id(),
            provider="codex",
            prompt="go",
            agent_name="backend-developer",
            task_id=task.id,
            run_id="18bb4c47-8575-5ab2-8b95-05b8ea9fc235",
        )
        run_storage.start(run.id)
        return run.id

    async def fake_cleanup_unattached_spawned_run(
        run_id: str,
        *,
        db: HubDatabase,
        error: str,
        completion_registry: object | None,
        terminal_services: object | None,
    ) -> bool:
        killed.append(run_id)
        assert db is temp_db
        assert completion_registry is None
        assert "disappeared before attach" in error
        run_storage.fail(run_id, error=f"dispatch mutex attach failed: {error}")
        return True

    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(dispatcher, "spawn_agent", fake_spawn_agent)
    monkeypatch.setattr(
        dispatcher,
        "_cleanup_unattached_spawned_run",
        fake_cleanup_unattached_spawned_run,
    )

    def fail_attach_run_id(
        self: TaskDispatchMutexManager,
        mutex_id: str,
        _run_id: str,
        _holder: str,
        *,
        now: datetime | str | None = None,
    ) -> bool:
        del now
        assert mutex_id == task.id
        self.force_release(mutex_id)
        return False

    monkeypatch.setattr(TaskDispatchMutexManager, "attach_run_id", fail_attach_run_id)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    run = run_storage.get("18bb4c47-8575-5ab2-8b95-05b8ea9fc235")
    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert killed == ["18bb4c47-8575-5ab2-8b95-05b8ea9fc235"]
    assert run is not None
    assert run.status == "error"
    assert storage.get_mutex(task.id) is None
    assert _required(task_manager.stage_states.get(task.id, "development")).state == "ready"
    assert updated.dispatch_failure_count == 1
    assert "dispatch_mutex_attach_failed" in (updated.description or "")


async def test_cancel_between_spawn_and_attach_terminalizes_run_before_redispatch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Cancellation in the spawn-attach window cleans up before redispatch."""
    from gobby.agents import kill as agent_kill
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")
    run_storage = LocalAgentRunManager(temp_db)
    run_ids = [
        "570221c9-b7ca-5db8-adb2-4fea57e49b71",
        "fe8689b8-c8bb-5670-bc26-a655c15147fa",
    ]
    spawned: list[str] = []
    killed: list[str] = []
    first_attach_started = asyncio.Event()
    original_run_db = dispatcher.run_db
    attach_attempts = 0

    def fake_spawn_agent(*_args: object, **_kwargs: object) -> str:
        run_id = run_ids[len(spawned)]
        run = run_storage.create(
            parent_session_id=system_session_id(),
            provider="codex",
            prompt="go",
            agent_name="backend-developer",
            task_id=task.id,
            run_id=run_id,
        )
        run_storage.start(run.id)
        spawned.append(run.id)
        return run.id

    async def fake_kill_agent(
        run: Any, db: HubDatabase, *, close_terminal: bool, terminal_services: object | None
    ) -> dict[str, bool]:
        assert db is temp_db
        assert close_terminal is True
        killed.append(run.id)
        return {"success": True}

    async def cancel_first_attach(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal attach_attempts
        if getattr(func, "__name__", None) == "attach":
            attach_attempts += 1
            if attach_attempts == 1:
                first_attach_started.set()
                await asyncio.Future()
        return await original_run_db(func, *args, **kwargs)

    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(dispatcher, "spawn_agent", fake_spawn_agent)
    monkeypatch.setattr(agent_kill, "kill_agent", fake_kill_agent)
    monkeypatch.setattr(dispatcher, "run_db", cancel_first_attach)

    heartbeat = asyncio.create_task(
        dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    )
    await first_attach_started.wait()
    heartbeat.cancel()

    with pytest.raises(asyncio.CancelledError):
        await heartbeat

    first_run = run_storage.get(run_ids[0])
    assert first_run is not None
    assert first_run.status == "error"
    assert killed == [run_ids[0]]
    assert storage.get_mutex(task.id) is None

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    second_run = run_storage.get(run_ids[1])
    assert result.executed == 1
    assert spawned == run_ids
    assert second_run is not None
    assert second_run.status == "running"
    assert _required(storage.get_mutex(task.id)).run_id == run_ids[1]


@pytest.mark.asyncio
async def test_spawn_action_uses_services_and_records_agent_run(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Spawn action uses services and records agent run."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, Any] = {}

    async def fake_spawn_agent_impl(**kwargs: Any) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=kwargs["parent_session_id"],
            provider="codex",
            prompt=kwargs["prompt"],
            agent_name=kwargs["agent_lookup_name"],
            task_id=task.id,
            run_id="2d6f8387-ee3f-5abb-98f4-70ace5661263",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    run = _required(LocalAgentRunManager(temp_db).get("2d6f8387-ee3f-5abb-98f4-70ace5661263"))
    launcher = _required(session_manager.get(run.parent_session_id))
    assert result.executed == 1
    assert run.agent_name == "backend-developer"
    assert run.task_id == task.id
    assert spawn_kwargs["task_id"] == task.id
    assert "_step_workflow_name" not in spawn_kwargs["initial_variables"]
    assert launcher.source == "dispatcher_launcher"
    assert _required(storage.get_mutex(task.id)).run_id == "2d6f8387-ee3f-5abb-98f4-70ace5661263"


@pytest.mark.parametrize("agent_slug", ["planner", "plan-adversary"])
@pytest.mark.asyncio
async def test_planning_agents_force_main_context(
    agent_slug: str,
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Planning agents run in the main context without mutating task artifacts."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="planning",
        stage_state="in_progress",
        isolation="worktree",
        assigned_agent=agent_slug,
    )
    stale_worktree_id = stable_test_uuid(f"wt-{agent_slug}")
    stale_worktree_path = f"/tmp/missing-{agent_slug}-worktree"
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=stale_worktree_path,
        worktree_id=stale_worktree_id,
        base_commit_sha="old-base",
        target_branch="main",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug=agent_slug,
        prompt="plan",
        initial_variables={"stage_name": "planning", "stage_state": "in_progress"},
    )
    spawn_kwargs: dict[str, object] = {}
    main_thread = threading.get_ident()
    evidence_threads: list[int] = []

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id=stable_test_uuid(f"run-{agent_slug}"),
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )

    def prepare_evidence(**kwargs: object) -> tuple[str, None, None]:
        evidence_threads.append(threading.get_ident())
        return str(kwargs["prompt"]), None, None

    monkeypatch.setattr(
        "gobby.dispatch.spawn._prepare_plan_adversary_evidence",
        prepare_evidence,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert spawn_kwargs["isolation"] == "none"
    assert spawn_kwargs["worktree_id"] is None
    assert spawn_kwargs["clone_id"] is None
    assert artifacts.worktree_id == stale_worktree_id
    assert artifacts.worktree_path == stale_worktree_path
    assert artifacts.base_commit_sha == "old-base"
    assert evidence_threads and evidence_threads[0] != main_thread


@pytest.mark.asyncio
async def test_expansion_review_forces_main_context(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Expansion review runs in the main context without workspace mutation."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="expansion",
        stage_state="needs_review",
        isolation="worktree",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/missing-expansion-worktree",
        worktree_id="9792b29b-aa8c-5633-a8cb-3cfe44f7de3d",
        base_commit_sha="old-base",
        target_branch="main",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="expansion-qa",
        prompt="review expansion",
        initial_variables={"stage_name": "expansion", "stage_state": "needs_review"},
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="27317c40-a771-5aa5-aff8-1ebe5a326f84",
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert spawn_kwargs["isolation"] == "none"
    assert spawn_kwargs["worktree_id"] is None
    assert spawn_kwargs["clone_id"] is None
    assert artifacts.worktree_id == "9792b29b-aa8c-5633-a8cb-3cfe44f7de3d"
    assert artifacts.worktree_path == "/tmp/missing-expansion-worktree"


@pytest.mark.asyncio
async def test_backend_developer_inherits_task_worktree_isolation(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Backend developer inherits task worktree isolation."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_state="in_progress",
        isolation="worktree",
        assigned_agent="backend-developer",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="2960b641-7fd2-51ec-9201-4dbf382eb21b",
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.executed == 1
    assert spawn_kwargs["isolation"] == "worktree"


@pytest.mark.asyncio
async def test_spawn_action_subscribes_build_coordinator_completion(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Spawn action subscribes build coordinator completion."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    coordinator = session_manager.register(
        external_id="coord-ext",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Coordinator",
    )
    task = _task(temp_db, sample_project, stage_state="in_progress")
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="f24030bc-390b-56fd-8e34-a11e20175c22",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    completion_registry = MagicMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        completion_registry=completion_registry,
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.executed == 1
    completion_registry.register.assert_called_once_with(
        "f24030bc-390b-56fd-8e34-a11e20175c22",
        subscribers=[coordinator.id],
    )
    subscribers = CompletionSubscriberManager(temp_db).get_completion_subscribers(
        "f24030bc-390b-56fd-8e34-a11e20175c22"
    )
    assert subscribers == [coordinator.id]


def test_spawn_action_skips_cross_project_build_coordinator_completion(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Spawn action skips cross project build coordinator completion."""
    from gobby.dispatch.spawn import _subscribe_build_coordinator_completion
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    other_project = LocalProjectManager(temp_db).create(name="other-project")
    session_manager = SessionManager(temp_db)
    coordinator = session_manager.register(
        external_id="coord-ext",
        machine_id=None,
        source="codex",
        project_id=other_project.id,
        title="Coordinator",
    )
    task = _task(temp_db, sample_project, stage_state="in_progress")
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
    )
    completion_registry = MagicMock()
    services = SimpleNamespace(
        session_manager=session_manager,
        completion_registry=completion_registry,
    )

    _subscribe_build_coordinator_completion(
        db=temp_db,
        project_id=sample_project["id"],
        task_id=task.id,
        run_id="56173d59-d5cc-563d-b801-379a49147505",
        services=services,
    )

    completion_registry.register.assert_not_called()
    subscribers = CompletionSubscriberManager(temp_db).get_completion_subscribers(
        "56173d59-d5cc-563d-b801-379a49147505"
    )
    assert subscribers == []


def test_spawn_action_allows_explicit_cross_project_build_coordinator_completion(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Spawn action subscribes a cross-project coordinator when build metadata authorizes it."""
    from gobby.dispatch.spawn import _subscribe_build_coordinator_completion
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    other_project = LocalProjectManager(temp_db).create(name="other-explicit-project")
    session_manager = SessionManager(temp_db)
    coordinator = session_manager.register(
        external_id="coord-ext-explicit",
        machine_id=None,
        source="codex",
        project_id=other_project.id,
        title="Coordinator",
    )
    task = _task(temp_db, sample_project, stage_state="in_progress")
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={
            "build_project_id": sample_project["id"],
            "coordinator_project_id": other_project.id,
            "coordinator_session_id": coordinator.id,
        },
    )
    completion_registry = MagicMock()
    services = SimpleNamespace(
        session_manager=session_manager,
        completion_registry=completion_registry,
    )

    _subscribe_build_coordinator_completion(
        db=temp_db,
        project_id=sample_project["id"],
        task_id=task.id,
        run_id="5bb56995-1d6b-5cf3-837f-eb2c4895cec7",
        services=services,
    )

    completion_registry.register.assert_called_once_with(
        "5bb56995-1d6b-5cf3-837f-eb2c4895cec7",
        subscribers=[coordinator.id],
    )
    subscribers = CompletionSubscriberManager(temp_db).get_completion_subscribers(
        "5bb56995-1d6b-5cf3-837f-eb2c4895cec7"
    )
    assert subscribers == [coordinator.id]


@pytest.mark.asyncio
async def test_spawn_action_without_coordinator_does_not_subscribe_launcher(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Spawn action without coordinator does not subscribe launcher."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="d3b630c8-9101-5a38-bd1a-2a2da5416055",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    completion_registry = MagicMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        completion_registry=completion_registry,
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.executed == 1
    completion_registry.register.assert_not_called()
    subscribers = CompletionSubscriberManager(temp_db).get_completion_subscribers(
        "d3b630c8-9101-5a38-bd1a-2a2da5416055"
    )
    assert subscribers == []


@pytest.mark.asyncio
async def test_spawn_action_clears_missing_worktree_artifact_before_reuse(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Spawn action clears missing worktree artifact before reuse."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_state="in_progress",
        isolation="worktree",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/missing-worktree",
        worktree_id="4d03aefe-edeb-5f9f-92dc-b3537f07d7bc",
        base_commit_sha="old-base",
        target_branch="main",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: Any) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=kwargs["parent_session_id"],
            provider="codex",
            prompt=kwargs["prompt"],
            agent_name=kwargs["agent_lookup_name"],
            task_id=task.id,
            run_id="d734fd14-a12a-5465-bcca-6d3c8f03c4f6",
        )
        return {"success": True, "run_id": run.id, "isolation": "worktree"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert spawn_kwargs["worktree_id"] is None
    assert artifacts.worktree_id is None
    assert artifacts.worktree_path is None
    assert artifacts.base_commit_sha is None
    assert artifacts.target_branch == "main"


@pytest.mark.asyncio
async def test_leaf_spawn_recovers_parent_integration_target_branch(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_git_project: dict[str, Any]
) -> None:
    """Leaf spawn recovers parent integration target branch."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sample_project = sample_git_project
    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    parent = _task(
        temp_db,
        sample_project,
        title="Phase epic",
        task_type="epic",
        allow_automation=False,
    )
    leaf = _task(
        temp_db,
        sample_project,
        title="Leaf implementation",
        parent_task_id=parent.id,
        stage_state="in_progress",
        isolation="worktree",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        parent.id,
        target_branch="main",
        integration_branch="gobby/integration/phase",
    )
    subprocess.run(
        ["git", "branch", "gobby/integration/phase"],
        cwd=sample_git_project["repo_path"],
        check=True,
    )
    action = SpawnAgentAction(
        task_id=leaf.id,
        task_ref=f"#{leaf.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    def unexpected_prepare(**_kwargs: object) -> None:
        raise AssertionError("leaf spawn should not use epic QA workspace preparation")

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=leaf.id,
            run_id="d0dce129-9dd1-505a-8bab-30728f041b21",
        )
        return {"success": True, "run_id": run.id, "isolation": "worktree"}

    monkeypatch.setattr(
        "gobby.dispatch.spawn_artifacts.ensure_epic_integration_workspaces",
        unexpected_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(leaf.id)

    assert result.executed == 1
    assert spawn_kwargs["base_branch"] == "gobby/integration/phase"
    assert spawn_kwargs["worktree_id"] is None
    assert artifacts.target_branch == "gobby/integration/phase"


@pytest.mark.asyncio
async def test_leaf_spawn_skips_stale_parent_integration_branch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
) -> None:
    """Leaf spawn skips stale parent integration branch artifacts."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sample_project = sample_git_project
    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    root = _task(
        temp_db,
        sample_project,
        title="Build target root",
        task_type="epic",
        allow_automation=False,
    )
    parent = _task(
        temp_db,
        sample_project,
        title="Closed parent epic",
        parent_task_id=root.id,
        task_type="epic",
        allow_automation=False,
        status="closed",
    )
    leaf = _task(
        temp_db,
        sample_project,
        title="Reopened leaf",
        parent_task_id=parent.id,
        stage_state="in_progress",
        isolation="worktree",
    )
    task_artifacts = TaskArtifactManager(temp_db)
    task_artifacts.set_artifacts_atomic(
        root.id,
        target_branch="main",
        integration_branch="7d32d628-8a3a-52c4-82cd-9da5f4e79812",
    )
    task_artifacts.set_artifacts_atomic(
        parent.id,
        target_branch="main",
        integration_branch="missing-parent-integration",
    )
    task_artifacts.set_artifacts_atomic(leaf.id, target_branch="missing-parent-integration")
    action = SpawnAgentAction(
        task_id=leaf.id,
        task_ref=f"#{leaf.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    def fake_ref_resolves(**kwargs: object) -> bool:
        return kwargs["ref_name"] != "missing-parent-integration"

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=leaf.id,
            run_id="4a504cc0-f680-5db4-ae37-50e10b3d062e",
        )
        return {"success": True, "run_id": run.id, "isolation": "worktree"}

    monkeypatch.setattr("gobby.dispatch.spawn_artifacts._artifact_ref_resolves", fake_ref_resolves)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = task_artifacts.get_artifacts(leaf.id)

    assert result.executed == 1
    assert spawn_kwargs["base_branch"] == "7d32d628-8a3a-52c4-82cd-9da5f4e79812"
    assert artifacts.target_branch == "7d32d628-8a3a-52c4-82cd-9da5f4e79812"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("branch_sha", "reason"),
    [
        (None, "merge_ready_task_branch_missing"),
        ("same-sha", "merge_ready_task_branch_matches_target"),
    ],
)
async def test_merge_ready_leaf_spawn_blocks_contaminated_task_branch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    branch_sha: str | None,
    reason: str,
) -> None:
    """Merge-ready leaf spawn refuses to recreate or reuse contaminated task branches."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import DispatchSpawnFailed, spawn_agent
    from gobby.storage.sessions import SessionManager

    sample_project = sample_git_project
    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    parent = _task(
        temp_db,
        sample_project,
        title="Phase epic",
        task_type="epic",
        allow_automation=False,
    )
    leaf = _task(
        temp_db,
        sample_project,
        title="Leaf implementation",
        parent_task_id=parent.id,
        stage_state="review_approved",
        isolation="worktree",
    )
    task_artifacts = TaskArtifactManager(temp_db)
    task_artifacts.set_artifacts_atomic(
        parent.id,
        target_branch="main",
        integration_branch="gobby/integration/phase",
    )
    subprocess.run(
        ["git", "branch", "gobby/integration/phase"],
        cwd=sample_git_project["repo_path"],
        check=True,
    )
    task_artifacts.set_artifacts_atomic(leaf.id, target_branch="gobby/integration/phase")
    action = SpawnAgentAction(
        task_id=leaf.id,
        task_ref=f"#{leaf.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )

    def fake_ref_sha(**kwargs: object) -> tuple[bool, str | None]:
        if kwargs["ref_name"] == "gobby/integration/phase":
            return True, "same-sha"
        return True, branch_sha

    async def unexpected_spawn_agent_impl(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("merge-ready contaminated branch must fail before spawn")

    monkeypatch.setattr("gobby.dispatch.spawn_artifacts._artifact_ref_sha", fake_ref_sha)
    monkeypatch.setattr(
        "gobby.dispatch.spawn_artifacts.ensure_task_parent_integration_workspace",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        unexpected_spawn_agent_impl,
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    with pytest.raises(DispatchSpawnFailed, match=reason):
        await spawn_agent(action, db=temp_db, services=services)


@pytest.mark.asyncio
async def test_epic_qa_spawn_refreshes_and_reuses_integration_workspace(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Epic QA spawn refreshes and reuses integration workspace."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="epic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="epic-reviewer",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/stale-parent",
        worktree_id="e94c1183-5264-5d4d-87c4-6f5b624fe827",
        base_commit_sha="old-base",
        target_branch="main",
        integration_branch="gobby/integration/parent",
        integration_workspace_id="de982dee-65f9-5a31-a035-b8016c3cd62b",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="epic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "epic_qa", "stage_state": "in_progress"},
    )
    prepare_calls: list[dict[str, object]] = []
    spawn_kwargs: dict[str, object] = {}

    def fake_prepare(**kwargs: object) -> None:
        prepare_calls.append(kwargs)

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="b468f448-f935-5d46-bd42-b4f74fb493d6",
        )
        return {
            "success": True,
            "run_id": run.id,
            "isolation": "worktree",
            "worktree_id": kwargs["worktree_id"],
            "worktree_path": "/tmp/integration-parent",
        }

    monkeypatch.setattr(
        "gobby.dispatch.spawn_artifacts.ensure_epic_integration_workspaces",
        fake_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert prepare_calls
    root_task = cast(_HasId, prepare_calls[0]["root_task"])
    assert root_task.id == task.id
    assert prepare_calls[0]["repair_only"] is True
    assert prepare_calls[0]["merge_closed_descendant_commits"] is True
    assert spawn_kwargs["worktree_id"] == "de982dee-65f9-5a31-a035-b8016c3cd62b"
    assert spawn_kwargs["clone_id"] is None
    assert artifacts.worktree_id is None
    assert artifacts.worktree_path is None
    assert artifacts.base_commit_sha is None


def test_epic_qa_spawn_rejects_unprovisioned_epic(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Epic QA refuses to create a workspace for an untouched epic."""
    from gobby.dispatch.spawn_artifacts import _prepare_spawn_artifacts
    from gobby.dispatch.spawn_errors import DispatchSpawnFailed

    task_manager = LocalTaskManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="epic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="epic-reviewer",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        target_branch="main",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="epic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "epic_qa", "stage_state": "in_progress"},
    )

    with pytest.raises(
        DispatchSpawnFailed,
        match=f"epic integration workspace has not been provisioned for #{task.seq_num}",
    ):
        _prepare_spawn_artifacts(
            db=temp_db,
            action=action,
            task=task,
            task_manager=task_manager,
            project_id=sample_project["id"],
            services=None,
            isolation="worktree",
        )

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.integration_branch is None
    assert artifacts.integration_workspace_id is None


def test_leaf_spawn_translates_missing_root_target(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Leaf workspace preparation exposes missing root target metadata as a spawn failure."""
    from gobby.dispatch.spawn_artifacts import _prepare_spawn_artifacts
    from gobby.dispatch.spawn_errors import DispatchSpawnFailed

    task_manager = LocalTaskManager(temp_db)
    root = _task(
        temp_db,
        sample_project,
        title="Root without target",
        task_type="epic",
        allow_automation=False,
    )
    leaf = _task(
        temp_db,
        sample_project,
        title="Leaf implementation",
        parent_task_id=root.id,
        stage_state="in_progress",
        isolation="worktree",
    )
    action = SpawnAgentAction(
        task_id=leaf.id,
        task_ref=f"#{leaf.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )

    with pytest.raises(
        DispatchSpawnFailed,
        match=f"target_branch is required for root epic integration workspace #{root.seq_num}",
    ):
        _prepare_spawn_artifacts(
            db=temp_db,
            action=action,
            task=leaf,
            task_manager=task_manager,
            project_id=sample_project["id"],
            services=None,
            isolation="worktree",
        )


def test_epic_qa_spawn_rejects_missing_root_target(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
) -> None:
    """Epic QA requires the target metadata recorded by the build lifecycle."""
    from gobby.dispatch.spawn_artifacts import _prepare_spawn_artifacts
    from gobby.dispatch.spawn_errors import DispatchSpawnFailed

    sample_project = sample_git_project
    task_manager = LocalTaskManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="epic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="epic-reviewer",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        integration_branch="gobby/integration/phase",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="epic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "epic_qa", "stage_state": "in_progress"},
    )

    with pytest.raises(
        DispatchSpawnFailed,
        match=f"target_branch is required for epic integration workspace #{task.seq_num}",
    ):
        _prepare_spawn_artifacts(
            db=temp_db,
            action=action,
            task=task,
            task_manager=task_manager,
            project_id=sample_project["id"],
            services=None,
            isolation="worktree",
        )


@pytest.mark.asyncio
async def test_epic_qa_workspace_conflict_rolls_back_without_heartbeat_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Epic QA workspace conflict rolls back without heartbeat error."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.build.workspaces import BuildWorkspaceError
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="epic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="epic-reviewer",
        dispatch_failure_count=2,
    )
    child = _task(
        temp_db,
        sample_project,
        title="Conflicting child",
        parent_task_id=task.id,
        stage_name="development",
        stage_state="done",
        task_type="feature",
        isolation="worktree",
        assigned_agent="backend-developer",
        status="closed",
    )
    set_stage_state(temp_db, task.id, "epic_qa", "in_progress", work_attempt_count=3)
    set_stage_state(temp_db, child.id, "development", "done", work_attempt_count=3)
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        target_branch="main",
        integration_branch="gobby/integration/phase",
        integration_workspace_id="de982dee-65f9-5a31-a035-b8016c3cd62b",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="epic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "epic_qa", "stage_state": "in_progress"},
    )

    def fail_prepare(**_kwargs: object) -> None:
        raise BuildWorkspaceError("failed to refresh integration workspace: CONFLICT")

    async def unexpected_spawn_agent_impl(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("spawn should not run after workspace preparation failure")

    monkeypatch.setattr(
        "gobby.dispatch.spawn_artifacts.ensure_epic_integration_workspaces",
        fail_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        unexpected_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )
    storage = _mutex_storage(temp_db)
    caplog.set_level(logging.ERROR, logger="gobby.dispatch.dispatcher")

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    updated = get_task(temp_db, task.id)
    reopened_child = get_task(temp_db, child.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    parent_stage = task_manager.stage_states.get(task.id, "epic_qa")
    child_stage = task_manager.stage_states.get(child.id, "development")
    assert parent_stage is not None
    assert child_stage is not None
    assert parent_stage.state == "ready"
    assert parent_stage.work_attempt_count == 2
    assert child_stage.state == "ready"
    assert child_stage.work_attempt_count == 0
    assert reopened_child.closed_at is None
    assert updated.dispatch_failure_count == 0
    assert updated.is_escalated is False
    assert "### Dispatch spawn failed" in (updated.description or "")
    assert "failed to refresh integration workspace: CONFLICT" in (updated.description or "")
    assert "Dispatcher heartbeat candidate failed" not in caplog.text


@pytest.mark.asyncio
async def test_spawn_failure_rolls_stage_ready_and_releases(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Spawn failure rolls stage ready and releases."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs: Any) -> dict[str, object]:
        return {"success": False, "error": "tmux unavailable"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert _required(task_manager.stage_states.get(task.id, "development")).state == "ready"
    assert updated.dispatch_failure_count == 1
    assert "### Dispatch spawn failed" in (updated.description or "")
    assert LocalAgentRunManager(temp_db).get("2d6f8387-ee3f-5abb-98f4-70ace5661263") is None


@pytest.mark.parametrize("kill_outcome", ["success", "returned_failure", "raised"])
@pytest.mark.parametrize("artifact_failure_point", ["read", "read_runtime", "write"])
async def test_artifact_persistence_failure_terminalizes_or_quarantines_before_redispatch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    kill_outcome: str,
    artifact_failure_point: str,
) -> None:
    """Post-spawn artifact failures cannot leave an active orphaned run."""
    from gobby.agents import kill as agent_kill
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher, spawn_artifacts
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager, ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")
    run_storage = LocalAgentRunManager(temp_db)
    run_ids = [
        "31e864df-cdf6-5a37-85f2-c3270f226f14",
        "61125fd1-d82d-533b-b16a-af4bde641d29",
    ]
    spawned: list[str] = []
    killed: list[str] = []

    async def fake_spawn_agent_impl(**_kwargs: object) -> dict[str, object]:
        run_id = run_ids[len(spawned)]
        run = run_storage.create(
            parent_session_id=system_session_id(),
            provider="codex",
            prompt="go",
            agent_name="backend-developer",
            task_id=task.id,
            run_id=run_id,
        )
        run_storage.start(run.id)
        spawned.append(run.id)
        result: dict[str, object] = {"success": True, "run_id": run.id}
        if len(spawned) == 1:
            result.update(
                worktree_id="81574289-ce50-5388-9583-e3d8b770c35d",
                worktree_path="/tmp/orphaned-dispatch-worktree",
                base_commit_sha="base-sha",
            )
        return result

    def fail_set_artifacts_atomic(*_args: object, **_kwargs: object) -> None:
        raise ValueError("injected artifact persistence failure")

    original_get_artifacts = TaskArtifactManager.get_artifacts
    artifact_read_failed = False

    def fail_first_post_spawn_artifact_read(
        manager: TaskArtifactManager,
        task_id: str,
    ) -> Any:
        nonlocal artifact_read_failed
        if spawned and not artifact_read_failed:
            artifact_read_failed = True
            if artifact_failure_point == "read_runtime":
                raise RuntimeError("injected non-database artifact read failure")
            raise psycopg.errors.SerializationFailure("injected artifact read failure")
        return original_get_artifacts(manager, task_id)

    async def fake_kill_agent(
        run: Any, db: HubDatabase, *, close_terminal: bool, terminal_services: object | None
    ) -> dict[str, object]:
        assert db is temp_db
        assert close_terminal is True
        killed.append(str(run.id))
        if kill_outcome == "raised":
            raise RuntimeError("injected kill failure")
        return {
            "success": kill_outcome == "success",
            "error": "injected unconfirmed termination",
        }

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    if artifact_failure_point.startswith("read"):
        monkeypatch.setattr(
            TaskArtifactManager,
            "get_artifacts",
            fail_first_post_spawn_artifact_read,
        )
    else:
        monkeypatch.setattr(spawn_artifacts, "_set_artifacts_atomic", fail_set_artifacts_atomic)
    monkeypatch.setattr(agent_kill, "kill_agent", fake_kill_agent)
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    first_result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
        max_actions=1,
    )

    first_run = run_storage.get(run_ids[0])
    assert first_result.executed == 1
    assert first_run is not None
    assert killed == [run_ids[0]]
    if kill_outcome == "success":
        assert first_run.status == "error"
        assert storage.get_mutex(task.id) is None
        assert _required(task_manager.stage_states.get(task.id, "development")).state == "ready"
    else:
        assert first_run.status == "running"
        assert _required(storage.get_mutex(task.id)).run_id == run_ids[0]
        assert (
            _required(task_manager.stage_states.get(task.id, "development")).state == "in_progress"
        )
        quarantined_task = task_manager.get_task(task.id)
        assert quarantined_task.is_escalated is True
        assert quarantined_task.escalation_reason == (
            f"dispatch_spawn_cleanup_unconfirmed:{run_ids[0]}"
        )

    second_result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
        max_actions=1,
    )

    if kill_outcome == "success":
        second_run = run_storage.get(run_ids[1])
        assert second_result.executed == 1
        assert spawned == run_ids
        assert second_run is not None
        assert second_run.status == "running"
        assert _required(storage.get_mutex(task.id)).run_id == run_ids[1]
    else:
        assert second_result.executed == 0
        assert spawned == [run_ids[0]]
        assert run_storage.get(run_ids[1]) is None


@pytest.mark.asyncio
async def test_spawn_quarantine_escalates_when_reattach_and_audit_fail(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Quarantine remains durable when mutex reattach and audit both fail."""
    from gobby.agents import kill as agent_kill
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager, ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")
    run_storage = LocalAgentRunManager(temp_db)
    run_ids = [
        "84ed7787-0c89-575c-b6bc-3719c653a29a",
        "44ef42c0-f4fa-5abe-b08c-bc66bc671e29",
    ]
    spawned: list[str] = []
    artifact_read_failed = False
    original_get_artifacts = TaskArtifactManager.get_artifacts

    async def fake_spawn_agent_impl(**_kwargs: object) -> dict[str, object]:
        run_id = run_ids[len(spawned)]
        run = run_storage.create(
            parent_session_id=system_session_id(),
            provider="codex",
            prompt="go",
            agent_name="backend-developer",
            task_id=task.id,
            run_id=run_id,
        )
        run_storage.start(run.id)
        spawned.append(run.id)
        return {
            "success": True,
            "run_id": run.id,
            "worktree_id": "9d131c8e-63f5-5f4b-8a67-8a87324d03fb",
            "worktree_path": "/tmp/quarantined-dispatch-worktree",
            "base_commit_sha": "base-sha",
        }

    def fail_first_post_spawn_artifact_read(
        manager: TaskArtifactManager,
        task_id: str,
    ) -> Any:
        nonlocal artifact_read_failed
        if spawned and not artifact_read_failed:
            artifact_read_failed = True
            raise RuntimeError("injected artifact read failure")
        return original_get_artifacts(manager, task_id)

    async def unconfirmed_kill(
        _run: Any,
        _db: HubDatabase,
        *,
        close_terminal: bool,
    ) -> dict[str, object]:
        assert close_terminal is True
        return {"success": False, "error": "injected unconfirmed termination"}

    def fail_mutex_reattach(*_args: object, **_kwargs: object) -> bool:
        raise psycopg.errors.SerializationFailure("injected quarantine reattach failure")

    async def fail_audit(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("injected quarantine audit failure")

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(
        TaskArtifactManager,
        "get_artifacts",
        fail_first_post_spawn_artifact_read,
    )
    monkeypatch.setattr(agent_kill, "kill_agent", unconfirmed_kill)
    monkeypatch.setattr(TaskDispatchMutexManager, "attach_run_id", fail_mutex_reattach)
    monkeypatch.setattr(dispatcher, "append_audit_marker", fail_audit)
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    first_result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
        max_actions=1,
    )

    first_run = run_storage.get(run_ids[0])
    quarantined_task = task_manager.get_task(task.id)
    assert first_result.executed == 1
    assert first_run is not None
    assert first_run.status == "running"
    assert storage.get_mutex(task.id) is None
    assert quarantined_task.is_escalated is True
    assert quarantined_task.escalation_reason == (
        f"dispatch_spawn_cleanup_unconfirmed:{run_ids[0]}"
    )

    second_result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
        max_actions=1,
    )

    assert second_result.executed == 0
    assert spawned == [run_ids[0]]
    assert run_storage.get(run_ids[1]) is None


@pytest.mark.asyncio
async def test_repeatedly_cancelled_spawn_cleanup_quarantines_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Repeated cancellation waits for unconfirmed termination to become quarantined."""
    from gobby.agents import kill as agent_kill
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")
    run_storage = LocalAgentRunManager(temp_db)
    run_id = "c86fcbb1-49ca-580a-b7d8-115194dd1aa7"
    spawned: list[str] = []
    kill_tasks: list[asyncio.Task[Any]] = []
    kill_started = asyncio.Event()
    allow_kill_result = asyncio.Event()

    async def fake_spawn_agent(*_args: object, **_kwargs: object) -> str:
        run = run_storage.create(
            parent_session_id=system_session_id(),
            provider="codex",
            prompt="go",
            agent_name="backend-developer",
            task_id=task.id,
            run_id=run_id,
        )
        run_storage.start(run.id)
        spawned.append(run.id)
        raise DispatchSpawnFailed(
            "injected post-spawn persistence failure",
            spawned_run_id=run.id,
        )

    async def fake_kill_agent(
        run: Any,
        db: HubDatabase,
        *,
        close_terminal: bool,
        terminal_services: object | None,
    ) -> dict[str, object]:
        assert run.id == run_id
        assert db is temp_db
        assert close_terminal is True
        current_task = asyncio.current_task()
        assert current_task is not None
        kill_tasks.append(current_task)
        kill_started.set()
        await allow_kill_result.wait()
        return {"success": False, "error": "termination remains unconfirmed"}

    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(dispatcher, "spawn_agent", fake_spawn_agent)
    monkeypatch.setattr(agent_kill, "kill_agent", fake_kill_agent)

    heartbeat = asyncio.create_task(
        dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    )
    await kill_started.wait()
    heartbeat.cancel()
    second_cancellation_requested = asyncio.Event()

    def cancel_again() -> None:
        heartbeat.cancel()
        second_cancellation_requested.set()

    asyncio.get_running_loop().call_soon(cancel_again)
    await second_cancellation_requested.wait()
    assert heartbeat.done() is False
    assert kill_tasks[0] is not heartbeat
    assert kill_tasks[0].cancelling() == 0

    allow_kill_result.set()
    with pytest.raises(asyncio.CancelledError):
        await heartbeat

    run = run_storage.get(run_id)
    quarantined_task = task_manager.get_task(task.id)
    mutex = storage.get_mutex(task.id)
    assert run is not None
    assert run.status == "running"
    assert mutex is not None
    assert mutex.run_id == run_id
    assert quarantined_task.is_escalated is True
    assert quarantined_task.escalation_reason == f"dispatch_spawn_cleanup_unconfirmed:{run_id}"

    second_result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert second_result.executed == 0
    assert spawned == [run_id]


@pytest.mark.asyncio
async def test_inner_spawn_cleanup_cancellation_quarantines_without_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation from cleanup itself is quarantined and propagated once."""
    from gobby.dispatch import spawn_actions

    action = SpawnAgentAction("task-id", "#1", "backend-developer", "go")
    quarantine_calls: list[str] = []
    shield_calls = 0

    async def cancelled_cleanup(*_args: object, **_kwargs: object) -> bool:
        raise asyncio.CancelledError("cleanup cancelled internally")

    async def quarantine(
        _action: SpawnAgentAction,
        *,
        run_id: str,
        **_kwargs: object,
    ) -> None:
        quarantine_calls.append(run_id)

    async def bounded_shield(task: asyncio.Task[bool]) -> bool:
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls > 1:
            raise AssertionError("cancelled cleanup task was awaited repeatedly")
        return await task

    monkeypatch.setattr("gobby.dispatch.spawn_actions.asyncio.shield", bounded_shield)

    with pytest.raises(asyncio.CancelledError, match="cleanup cancelled internally"):
        await spawn_actions._cleanup_or_quarantine_spawned_run(
            action,
            run_id="spawned-run-id",
            mutex=MagicMock(),
            db=MagicMock(),
            error="post-spawn cleanup failed",
            completion_registry=None,
            terminal_services=None,
            cleanup_unattached_spawned_run=cancelled_cleanup,
            quarantine_unterminated_spawned_run=quarantine,
        )

    assert shield_calls == 1
    assert quarantine_calls == ["spawned-run-id"]


@pytest.mark.asyncio
async def test_spawn_unavailable_does_not_mark_task_failed(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Spawn unavailable does not mark task failed."""
    from gobby.dispatch import dispatcher

    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    updated = get_task(temp_db, task.id)
    assert result.executed == 0
    assert result.skipped == 1
    assert result.reason == "services_missing:database,task_manager,session_manager,agent_runner"
    assert storage.get_mutex(task.id) is None
    assert _required(task_manager.stage_states.get(task.id, "development")).state == "in_progress"
    assert updated.dispatch_failure_count == 0
    assert "### Dispatch spawn failed" not in (updated.description or "")


@pytest.mark.asyncio
async def test_unregistered_spawn_records_dispatch_failure_telemetry(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Unregistered spawn records dispatch failure telemetry."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs: Any) -> dict[str, object]:
        return {"success": False, "error": "agent_did_not_register"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert _required(task_manager.stage_states.get(task.id, "development")).state == "ready"
    assert updated.claimed_by_session_id is None
    assert updated.dispatch_failure_count == 1
    assert "### Dispatch spawn failed" in (updated.description or "")
    assert "agent_did_not_register" in (updated.description or "")


@pytest.mark.asyncio
async def test_spawn_failure_cleanup_tolerates_already_ready_stage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Spawn failure cleanup tolerates already ready stage."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks._stage_types import IllegalStageTransitionError

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs: Any) -> dict[str, object]:
        return {"success": False, "error": "code_index_preflight_failed"}

    def racing_fail_stage(*_args: Any, **_kwargs: Any) -> NoReturn:
        set_stage_state(temp_db, task.id, "development", "ready")
        raise IllegalStageTransitionError("development", "ready", "fail_stage", "required")

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(task_manager.stage_states, "fail_stage", racing_fail_stage)
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    with caplog.at_level(logging.WARNING):
        result = await dispatcher.run_heartbeat(
            db=temp_db,
            project_id=sample_project["id"],
            services=services,
        )

    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert _required(task_manager.stage_states.get(task.id, "development")).state == "ready"
    assert updated.dispatch_failure_count == 1
    assert "### Dispatch spawn failed" in (updated.description or "")
    assert "Failed to roll back stage after dispatch spawn failure" not in caplog.text


@pytest.mark.asyncio
async def test_third_spawn_failure_escalates(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Third spawn failure escalates."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress", dispatch_failure_count=2)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs: Any) -> dict[str, object]:
        return {"success": False, "error": "broken"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)

    updated = get_task(temp_db, task.id)
    assert updated.is_escalated is True
    assert (updated.escalation_reason or "").startswith("dispatch_spawn_max_attempts:broken")


@pytest.mark.asyncio
async def test_spawn_prefers_project_scoped_git_manager(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Spawn prefers project scoped git manager."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="dev")

    default_repo = tmp_path / "default-repo"
    project_repo = tmp_path / "350154ff-e994-55b0-a88a-d9de7970cbc5"
    default_repo.mkdir()
    project_repo.mkdir()
    default_git = SimpleNamespace(repo_path=str(default_repo))
    project_git = SimpleNamespace(repo_path=str(project_repo))
    default_clone = object()
    captured: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {"success": True, "run_id": "aa37aedd-eea4-5c79-a039-aa80a4a17195"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
        git_manager=default_git,
        clone_manager=default_clone,
        get_git_manager=lambda project_id: project_git,
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        services=services,
    )

    assert run_id == "aa37aedd-eea4-5c79-a039-aa80a4a17195"
    assert captured["git_manager"] is project_git
    assert captured["clone_manager"] is not default_clone
    clone_manager = cast(_HasRepoPath, captured["clone_manager"])
    assert str(clone_manager.repo_path) == project_git.repo_path
    assert captured["base_branch"] == "dev"


@pytest.mark.asyncio
async def test_dispatch_spawn_uses_task_project_context_for_cross_project_build(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Dispatcher-spawned agents use the target task project, not the caller project."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.sessions import SessionManager
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    sync_bundled_agents(temp_db)
    caller = install_isolated_checkout_project(
        temp_db, tmp_path / "caller", name="caller-project", monkeypatch=monkeypatch
    )
    target = install_isolated_checkout_project(
        temp_db, tmp_path / "target", name="target-project", monkeypatch=monkeypatch
    )
    caller_project = caller.project
    target_project = target.project
    monkeypatch.setattr(
        "gobby.agents.launcher_session.require_machine_id",
        lambda: target.machine_id,
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=target_project.id,
        title="Target project task",
        task_type="task",
        category="code",
        allow_automation=True,
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    captured: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {"success": True, "run_id": "4bd0f604-127d-596e-9130-67a92c34f6ef"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    sessions = SessionManager(temp_db)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=sessions,
        agent_runner=SimpleNamespace(),
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        context=SimpleNamespace(project_id=caller_project.id),
        services=services,
    )
    launcher = sessions.get(str(captured["parent_session_id"]))

    assert run_id == "4bd0f604-127d-596e-9130-67a92c34f6ef"
    assert captured["project_path"] == target.root_path
    assert launcher is not None
    assert launcher.project_id == target_project.id


@pytest.mark.asyncio
async def test_dispatch_spawn_uses_machine_checkout(  # tdd-red window
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.sessions import SessionManager
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    sync_bundled_agents(temp_db)
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "spawn-checkout", name="spawn-checkout", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(
        "gobby.agents.launcher_session.require_machine_id",
        lambda: isolated.machine_id,
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=isolated.project.id,
        title="Spawn checkout task",
        task_type="task",
        category="code",
        allow_automation=True,
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    captured: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {"success": True, "run_id": "aa37aedd-eea4-5c79-a039-aa80a4a17195"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    sessions = SessionManager(temp_db)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=sessions,
        agent_runner=SimpleNamespace(),
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        services=services,
    )

    assert run_id == "aa37aedd-eea4-5c79-a039-aa80a4a17195"
    assert captured["project_path"] == isolated.root_path


@pytest.mark.asyncio
async def test_dispatch_spawn_uses_registered_overlay_cwd(  # tdd-red window
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    sync_bundled_agents(temp_db)
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    monkeypatch.setattr("gobby.storage.worktrees.require_machine_id", lambda: machine_id)
    monkeypatch.setattr("gobby.agents.launcher_session.require_machine_id", lambda: machine_id)
    project = LocalProjectManager(temp_db).create("spawn-overlay")
    overlay = tmp_path / "spawn-worktree"
    overlay.mkdir()
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project.id,
        title="Spawn overlay task",
        task_type="task",
        category="code",
        allow_automation=True,
        isolation="worktree",
        validation_criteria="Test task completion is observable.",
    )
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=project.id,
        branch_name="task/overlay",
        worktree_path=str(overlay),
        task_id=task.id,
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=str(overlay),
        worktree_id=worktree.id,
        base_commit_sha="a" * 40,
    )
    captured: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {"success": True, "run_id": "bb37aedd-eea4-5c79-a039-aa80a4a17195"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    sessions = SessionManager(temp_db)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=sessions,
        agent_runner=SimpleNamespace(),
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        services=services,
    )

    assert run_id == "bb37aedd-eea4-5c79-a039-aa80a4a17195"
    assert captured["project_path"] == str(overlay)


@pytest.mark.asyncio
async def test_dispatch_spawn_fails_closed_without_checkout(  # tdd-red window
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    sync_bundled_agents(temp_db)
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create("spawn-missing-checkout")
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project.id,
        title="Spawn missing checkout",
        task_type="task",
        category="code",
        allow_automation=True,
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    sessions = SessionManager(temp_db)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=sessions,
        agent_runner=SimpleNamespace(),
    )

    with pytest.raises((CheckoutNotFoundError, DispatchSpawnFailed)):
        await spawn_agent(
            SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
            db=temp_db,
            services=services,
        )


@pytest.mark.asyncio
async def test_bad_candidate_is_skipped_and_next_candidate_executes(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Bad candidate is skipped and next candidate executes."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "first")
    second = _task(temp_db, sample_project, "second")
    executed: list[str] = []
    metric_outcomes: list[str] = []

    def action_for(task: Task, *_args: Any) -> AppendAuditMarkerAction:
        return _audit_action(task.id)

    async def flaky_execute(action: AppendAuditMarkerAction, **kwargs: Any) -> bool:
        if action.task_id == first.id:
            raise RuntimeError("bad candidate")
        executed.append(action.task_id)
        return await append_audit_marker(
            kwargs["db"],
            action.task_id,
            action.heading,
            action.body,
        )

    monkeypatch.setattr(dispatch_rules, "evaluate", action_for)
    monkeypatch.setattr(dispatcher, "execute_action", flaky_execute)

    def record_metric(component: str, outcome: str) -> None:
        metric_outcomes.append(f"{component}:{outcome}")

    monkeypatch.setattr(
        dispatcher,
        "record_automation_event",
        record_metric,
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert result.skipped == 1
    assert executed == [second.id]
    assert metric_outcomes == ["dispatcher:failed", "dispatcher:succeeded"]
    assert "### Dispatch failed" in (get_task(temp_db, first.id).description or "")


@pytest.mark.asyncio
async def test_transient_database_error_releases_and_skips_candidate(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A candidate-local database error does not starve later candidates."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "first")
    second = _task(temp_db, sample_project, "second")
    executed: list[str] = []

    def action_for(task: Task, *_args: Any) -> AppendAuditMarkerAction:
        return _audit_action(task.id)

    async def deadlocking_execute(action: AppendAuditMarkerAction, **kwargs: Any) -> bool:
        if action.task_id == first.id:
            raise psycopg.errors.DeadlockDetected("candidate deadlock")
        executed.append(action.task_id)
        return await append_audit_marker(
            kwargs["db"],
            action.task_id,
            action.heading,
            action.body,
        )

    monkeypatch.setattr(dispatch_rules, "evaluate", action_for)
    monkeypatch.setattr(dispatcher, "execute_action", deadlocking_execute)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert result.skipped == 1
    assert executed == [second.id]
    assert "### Dispatch failed" in (get_task(temp_db, first.id).description or "")
    assert _mutex_storage(temp_db).get_mutex(first.id) is None


@pytest.mark.asyncio
async def test_connection_database_error_aborts_candidate_scan(
    monkeypatch: pytest.MonkeyPatch, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A connection-level database error terminates the heartbeat scan."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "first")
    _task(temp_db, sample_project, "second")
    attempted: list[str] = []

    def action_for(task: Task, *_args: Any) -> AppendAuditMarkerAction:
        return _audit_action(task.id)

    async def disconnected_execute(action: AppendAuditMarkerAction, **_kwargs: Any) -> NoReturn:
        attempted.append(action.task_id)
        raise psycopg.errors.ConnectionException("connection lost")

    monkeypatch.setattr(dispatch_rules, "evaluate", action_for)
    monkeypatch.setattr(dispatcher, "execute_action", disconnected_execute)

    with pytest.raises(psycopg.errors.ConnectionException, match="connection lost"):
        await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert attempted == [first.id]
    assert _mutex_storage(temp_db).get_mutex(first.id) is None
    assert "### Dispatch failed" not in (get_task(temp_db, first.id).description or "")


@pytest.mark.asyncio
async def test_advance_action_releases_lease_immediately(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Advance action releases lease immediately."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_name="development")
    storage = _mutex_storage(temp_db)
    action = StartStageAction(
        task_id=task.id,
        stage_name="development",
    )
    monkeypatch.setattr(dispatch_rules, "evaluate", lambda *args, **kwargs: action)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_merge_workspace_action_releases_lease_before_stage_transition(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Merge actions release their dispatch lease before completing the merge stage."""
    from gobby.dispatch import dispatcher
    from gobby.storage.tasks._runtime_mutex import RuntimeDispatchMutex
    from tests.storage.tasks._stage_test_helpers import stage_row

    task = _task(
        temp_db,
        sample_project,
        stage_name="merge",
        stage_state="in_progress",
        isolation="worktree",
    )
    storage = _mutex_storage(temp_db)
    action = MergeWorkspaceAction(
        task_id=task.id,
        task_ref="#123",
        backend="worktree",
        target_branch="main",
        source_branch="feature/test",
    )

    async def complete_merge_stage(
        action: MergeWorkspaceAction,
        *,
        db: HubDatabase,
        services: object | None = None,
    ) -> object:
        assert storage.get_mutex(action.task_id) is None
        manager = dispatcher._stage_states_manager(db=db, services=services)
        return manager.complete_stage(action.task_id, "merge", by_session_id="dispatcher")

    monkeypatch.setattr(dispatcher, "execute_merge_workspace", complete_merge_stage)
    mutex = RuntimeDispatchMutex(
        storage,
        task.id,
        holder="dispatcher",
        action_kind="merge_workspace",
        ttl_seconds=600,
    )

    with mutex:
        await dispatcher.execute_action(action, mutex=mutex, db=temp_db)

    row = stage_row(temp_db, task.id, "merge")
    assert storage.get_mutex(task.id) is None
    assert row["state"] == "done"


@pytest.mark.asyncio
async def test_start_pipeline_action_links_execution_id(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Start pipeline action links execution id."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )
    services = SimpleNamespace(pipeline_executor=_FakePipelineExecutor())

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)

    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id is not None
    assert mutex.action_kind == "stage-pipeline:expansion"
    assert services.pipeline_executor.loader.project_ids == [sample_project["id"]]


def test_dispatcher_run_heartbeat_cold_imports(repo_root: Path) -> None:
    """Dispatcher run heartbeat cold imports."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gobby.dispatch.dispatcher import run_heartbeat; print(run_heartbeat.__name__)",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "run_heartbeat"


def test_dispatch_inputs_invalid_json_logs_debug(
    caplog: pytest.LogCaptureFixture,
    enable_log_propagation: None,
) -> None:
    """Dispatch inputs invalid json logs debug."""
    from gobby.dispatch import rules

    registry_entry = SimpleNamespace(
        id="63c76849-8ad5-5e57-b9b6-a362883e46c3",
        stage_name="expansion",
        dispatch_inputs_json='{"invalid"',
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.dispatch.rules"):
        assert rules._dispatch_inputs(registry_entry) == {}

    records = [
        record
        for record in caplog.records
        if record.message == "Invalid stage registry dispatch_inputs_json; ignoring"
    ]
    assert len(records) == 1
    record = cast(_DispatchInputsLogRecord, records[0])
    assert record.registry_entry == {
        "id": "63c76849-8ad5-5e57-b9b6-a362883e46c3",
        "stage_name": "expansion",
    }
    assert record.raw_dispatch_inputs_json == '{"invalid"'
    assert "Expecting" in record.error


def test_build_context_loads_stage_registry_and_bundled_agents(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Build context loads stage registry and bundled agents."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    task = _task(temp_db, sample_project, stage_name="pr", stage_state="in_progress")

    context = cast(_DispatchContext, dispatcher.build_context(temp_db, task))

    assert context.stage_registry["pr"].default_agent == "merge-orchestrator"
    assert context.agents["merge-orchestrator"].enabled is True
    assert context.agent_definitions["merge-orchestrator"].spawn_capable is True


def test_build_context_project_disabled_agent_override_wins(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Build context project disabled agent override wins."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.definitions.agents import AgentDefinitionManager
    from gobby.workflows.definitions import AgentDefinitionBody

    sync_bundled_agents(temp_db)
    AgentDefinitionManager(temp_db).create(
        name="merge-orchestrator",
        project_id=sample_project["id"],
        source="project",
        enabled=False,
        definition_json=AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-orchestrator",
            description="Project override",
            surfaces=["spawn"],
        ).model_dump_json(),
    )
    task = _task(temp_db, sample_project, stage_name="pr", stage_state="in_progress")

    context = cast(_DispatchContext, dispatcher.build_context(temp_db, task))

    assert context.agents["merge-orchestrator"].enabled is False
    assert context.agents["merge-orchestrator"].project_id == sample_project["id"]


@pytest.mark.asyncio
async def test_real_heartbeat_pr_stage_spawns_merge_orchestrator_without_false_no_agent(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Real heartbeat pr stage spawns merge orchestrator without false no agent."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    task = _task(temp_db, sample_project, stage_name="pr", stage_state="in_progress")
    spawned: list[str] = []

    def record_spawn(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append(action.agent_slug)
        return "39ad77a9-2925-5095-a22e-82412ecd6d0c"

    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert spawned == ["merge-orchestrator"]
    assert get_task(temp_db, task.id).is_escalated is False


@pytest.mark.asyncio
async def test_real_heartbeat_merge_ready_starts_then_spawns_merge_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Real heartbeat merge ready starts then spawns merge orchestrator."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Merge ready",
        task_type="feature",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    update_task(temp_db, task.id, allow_automation=True, isolation="none")
    initialize_manifest(temp_db, task.id, [spec("pr", 0), spec("merge", 1)])
    set_stage_state(temp_db, task.id, "pr", "done")
    set_stage_state(temp_db, task.id, "merge", "ready")
    spawned: list[str] = []

    def record_spawn(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append(action.agent_slug)
        return "1c750214-6550-592b-b1fd-0b01aa584ad0"

    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    first = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    second = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert first.executed == 1
    assert second.executed == 1
    assert _required(manager.stage_states.get(task.id, "merge")).state == "in_progress"
    assert spawned == ["merge-orchestrator"]


@pytest.mark.asyncio
async def test_dispatcher_starts_stage_pipeline_with_injected_services(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Dispatcher starts stage pipeline with injected services."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    _session(temp_db, sample_project, SESSION_1)
    executor = _FakePipelineExecutor()
    services = SimpleNamespace(
        pipeline_executor=executor,
        triggering_session_id=SESSION_1,
    )
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    async def record_background(*args: Any, **kwargs: Any) -> None:
        executor.record_call(
            {
                "inputs": args[2],
                "execution_id": args[4],
                "session_id": kwargs.get("session_id"),
            }
        )

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    calls = await _wait_for_executor_calls(executor)

    assert calls[0]["inputs"] == {"task_id": task.id}
    assert calls[0]["session_id"] == SESSION_1


@pytest.mark.asyncio
async def test_execution_id_attaches_before_background_pipeline_start(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Execution id attaches before background pipeline start."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    executor = _FakePipelineExecutor()
    services = SimpleNamespace(pipeline_executor=executor)
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    async def record_background(*args: Any, **kwargs: Any) -> None:
        executor.record_call({"execution_id": args[4]})

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    calls = await _wait_for_executor_calls(executor)

    execution_id = calls[0]["execution_id"]
    assert _required(storage.get_mutex(task.id)).run_id == execution_id


@pytest.mark.asyncio
async def test_pipeline_terminal_handler_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Pipeline terminal handler releases lease."""
    from gobby.dispatch import dispatcher
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    executor = _FakePipelineExecutor()
    services = SimpleNamespace(pipeline_executor=executor)
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    async def record_background(*args: Any, **kwargs: Any) -> None:
        executor.record_call({"execution_id": args[4]})

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    calls = await _wait_for_executor_calls(executor)
    _dispatch.on_pipeline_failed(
        {"execution_id": calls[0]["execution_id"], "error": "boom"},
        db=temp_db,
        storage=storage,
    )

    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_invalid_pipeline_target_escalates_and_releases(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Invalid pipeline target escalates and releases."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: StartPipelineAction(
            task_id=task.id,
            task_ref="#1",
            stage_name="expansion",
            pipeline_name="missing",
            dispatch_inputs={},
        ),
    )
    services = SimpleNamespace(pipeline_executor=_FakePipelineExecutor())

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)

    assert storage.get_mutex(task.id) is None
    assert get_task(temp_db, task.id).is_escalated is True


@pytest.mark.asyncio
async def test_stage_pipeline_runtime_mutex_failure_is_retry_neutral(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Internal stage-pipeline mutex races restore the stage without burning attempts."""
    from gobby.dispatch import dispatcher
    from gobby.dispatch.mutex import RuntimeDispatchMutexError
    from tests.storage.tasks._stage_test_helpers import lifecycle_events, stage_row

    task = _task(
        temp_db,
        sample_project,
        lifecycle="expanding",
        stage_state="in_progress",
        isolation="worktree",
    )
    set_stage_state(temp_db, task.id, "expansion", "in_progress", work_attempt_count=1)
    storage = _mutex_storage(temp_db)
    services = SimpleNamespace(pipeline_executor=_FakePipelineExecutor())
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    def fail_attach(*_args: object, **_kwargs: object) -> str:
        raise RuntimeDispatchMutexError(
            f"dispatch mutex for task {task.id!r} is held by another dispatcher"
        )

    monkeypatch.setattr(dispatcher, "_create_stage_pipeline_execution", fail_attach)

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    row = stage_row(temp_db, task.id, "expansion")
    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert row["state"] == "ready"
    assert row["work_attempt_count"] == 0
    assert updated.is_escalated is False
    assert updated.dispatch_failure_count == 0
    assert lifecycle_events(temp_db, task.id)[-1]["reason"].startswith(
        "stage_pipeline_dispatch_retry_neutral:"
    )


@pytest.mark.asyncio
async def test_create_isolation_action_writes_artifact_pair_and_base_commit_sha_atomically(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Create isolation action writes artifact pair and base commit sha atomically."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="main")
    monkeypatch.setattr(dispatcher, "resolve_branch_sha", lambda branch: "abc123")
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: CreateIsolationAction(
            task_id=task.id,
            task_ref="#1",
            isolation="worktree",
        ),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_path
    assert artifacts.worktree_id
    assert artifacts.base_commit_sha == "abc123"


@pytest.mark.asyncio
async def test_create_isolation_action_resolves_base_commit_sha_from_target_branch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Create isolation action resolves base commit sha from target branch."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="main")
    resolved: list[str] = []

    def resolve_branch_sha(branch: str) -> str:
        resolved.append(branch)
        return "abc123"

    monkeypatch.setattr(dispatcher, "resolve_branch_sha", resolve_branch_sha)
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: CreateIsolationAction(task.id, "#1", "worktree"),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert resolved == ["main"]


def test_persist_spawn_artifacts_writes_base_commit_sha(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Persist spawn artifacts writes base commit sha."""
    from gobby.dispatch.spawn import _persist_spawn_artifacts

    task = _task(temp_db, sample_project, isolation="worktree")

    _persist_spawn_artifacts(
        temp_db,
        task.id,
        {
            "worktree_id": "6a061cb3-f607-55f6-b3eb-04579360a44c",
            "worktree_path": "/tmp/worktree",
            "base_commit_sha": "base-sha",
        },
    )

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_id == "6a061cb3-f607-55f6-b3eb-04579360a44c"
    assert artifacts.worktree_path == "/tmp/worktree"
    assert artifacts.base_commit_sha == "base-sha"

    clone_task = _task(temp_db, sample_project, isolation="clone", title="Clone dispatch task")
    _persist_spawn_artifacts(
        temp_db,
        clone_task.id,
        {
            "clone_id": "b30ecba1-7be6-569c-af3e-57e430a37200",
            "clone_path": "/tmp/clone",
            "base_commit_sha": "ca11cf0f-a8d3-5854-9921-ef59d7946f2c",
        },
    )

    clone_artifacts = TaskArtifactManager(temp_db).get_artifacts(clone_task.id)
    assert clone_artifacts.clone_id == "b30ecba1-7be6-569c-af3e-57e430a37200"
    assert clone_artifacts.clone_path == "/tmp/clone"
    assert clone_artifacts.base_commit_sha == "ca11cf0f-a8d3-5854-9921-ef59d7946f2c"


def test_persist_spawn_artifacts_reraises_persistence_errors(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact persistence failures propagate to dispatcher callers."""
    from gobby.dispatch import spawn as spawn_module
    from gobby.dispatch import spawn_artifacts
    from gobby.dispatch.spawn import DispatchSpawnFailed

    task = _task(temp_db, sample_project, isolation="worktree")

    def fail_set_artifacts_atomic(*_args: object, **_kwargs: object) -> None:
        raise ValueError("bad artifact")

    monkeypatch.setattr(spawn_artifacts, "_set_artifacts_atomic", fail_set_artifacts_atomic)

    with pytest.raises(DispatchSpawnFailed, match="artifact_persistence_failed") as exc_info:
        spawn_module._persist_spawn_artifacts(
            temp_db,
            task.id,
            {
                "worktree_id": "6a061cb3-f607-55f6-b3eb-04579360a44c",
                "worktree_path": "/tmp/worktree",
                "base_commit_sha": "base-sha",
            },
        )
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert str(exc_info.value.__cause__) == "bad artifact"


def test_persist_spawn_artifacts_updates_standalone_base_commit_sha(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A spawn result may only refresh the base SHA for an existing workspace."""
    from gobby.dispatch.spawn import _persist_spawn_artifacts

    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_id="6a061cb3-f607-55f6-b3eb-04579360a44c",
        worktree_path="/tmp/worktree",
        base_commit_sha="old-base",
    )

    _persist_spawn_artifacts(temp_db, task.id, {"base_commit_sha": "new-base"})

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_id == "6a061cb3-f607-55f6-b3eb-04579360a44c"
    assert artifacts.worktree_path == "/tmp/worktree"
    assert artifacts.base_commit_sha == "new-base"


@pytest.mark.asyncio
async def test_dispatch_spawn_tolerates_build_coordinator_subscription_failure(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Agent spawn succeeds even when best-effort coordinator subscription fails."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, isolation="none")
    sessions = SessionManager(temp_db)
    coordinator = sessions.register(
        external_id="coordinator-subscribe-failure",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
    )

    async def fake_spawn_agent_impl(**_kwargs: Any) -> dict[str, object]:
        return {"success": True, "run_id": "2130ceda-1787-5c67-8ff4-7232d3b8fbd7"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(
        "gobby.dispatch.spawn_completion.subscribe_agent_completion",
        MagicMock(side_effect=RuntimeError("subscriber store unavailable")),
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=sessions,
        agent_runner=SimpleNamespace(),
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        services=services,
    )

    assert run_id == "2130ceda-1787-5c67-8ff4-7232d3b8fbd7"


@pytest.mark.asyncio
async def test_create_isolation_action_missing_target_branch_escalates(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Create isolation action missing target branch escalates."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, isolation="worktree")
    escalations: list[dict[str, object]] = []

    def record_escalation(**kwargs: object) -> None:
        escalations.append(kwargs)

    monkeypatch.setattr(dispatcher, "escalate_task", record_escalation)
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: CreateIsolationAction(task.id, "#1", "worktree"),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert escalations[0]["reason"] == "isolation_missing_target_branch"


@pytest.mark.asyncio
async def test_dev_rule_fires_after_isolation_and_stage_start(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Dev rule fires after isolation and stage start."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="main")
    spawned: list[str] = []
    monkeypatch.setattr(dispatcher, "resolve_branch_sha", lambda branch: "abc123")

    def record_spawn(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append(action.task_id)
        return "ac314d27-4314-5fe3-a0ab-01645086e137"

    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    first = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    second = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert first.executed == 1
    assert second.executed == 1
    assert spawned == [task.id]


@pytest.mark.asyncio
async def test_startup_sweep_clears_expired_leases(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Startup sweep clears expired leases."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, allow_automation=False)
    storage = _mutex_storage(temp_db)
    past = datetime.now(UTC) - timedelta(seconds=60)
    storage.acquire_mutex(task.id, holder="old", kind="test", ttl_seconds=1, now=past)
    temp_db.execute(
        """
        INSERT INTO integration_workspace_mutex (
            integration_key, lease_until, lease_holder, updated_at
        ) VALUES (%s, %s, %s, %s)
        """,
        ("worktree:integration/root", past, "dead-dispatcher", past),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], startup=True)

    assert storage.get_mutex(task.id) is None
    assert (
        temp_db.fetchone(
            "SELECT 1 FROM integration_workspace_mutex WHERE integration_key = %s",
            ("worktree:integration/root",),
        )
        is None
    )


@pytest.mark.asyncio
async def test_startup_sweep_preserves_expired_leases_for_active_runs(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Startup sweep preserves expired mutexes that still belong to active runs."""
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import ensure_system_session, system_session_id

    ensure_system_session(temp_db)
    active_task = _task(temp_db, sample_project, "Active", allow_automation=False)
    orphan_task = _task(temp_db, sample_project, "Orphan", allow_automation=False)
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=system_session_id(),
        provider="codex",
        prompt="work",
        task_id=active_task.id,
    )
    storage = _mutex_storage(temp_db)
    past = datetime.now(UTC) - timedelta(seconds=60)
    storage.acquire_mutex(
        active_task.id,
        holder="old",
        kind="test",
        ttl_seconds=1,
        run_id=run.id,
        now=past,
    )
    storage.acquire_mutex(
        orphan_task.id,
        holder="old",
        kind="test",
        ttl_seconds=1,
        now=past,
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], startup=True)

    assert storage.get_mutex(active_task.id) is not None
    assert storage.get_mutex(orphan_task.id) is None


@pytest.mark.asyncio
async def test_heartbeat_preserves_no_run_mutex_with_live_lease(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Heartbeat preserves no-run mutexes while their lease is still active."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    old_acquired_at = datetime.now(UTC) - timedelta(
        seconds=dispatcher.ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS + 5
    )
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=dispatcher.DISPATCH_TTL_SECONDS,
        now=old_acquired_at,
    )
    spawned: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: SpawnAgentAction(
            task_id=task.id,
            task_ref=f"#{task.seq_num}",
            agent_slug="backend-developer",
            prompt="resume work",
        ),
    )

    def record_spawn(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append((action.task_id, action.task_ref, action.agent_slug))
        return "ad91abd1-f0f0-527c-a037-2270467bb189"

    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    first = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert first.executed == 0
    assert first.skipped == 0
    assert spawned == []
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id is None

    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=1,
        now=old_acquired_at,
    )

    second = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert second.executed == 1
    assert spawned == [(task.id, f"#{task.seq_num}", "backend-developer")]
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id == "ad91abd1-f0f0-527c-a037-2270467bb189"


@pytest.mark.asyncio
async def test_heartbeat_recovers_expired_no_run_mutex(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Heartbeat recovers no-run mutexes after their lease expires."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    old_acquired_at = datetime.now(UTC) - timedelta(
        seconds=dispatcher.ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS + 5
    )
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=1,
        now=old_acquired_at,
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: SpawnAgentAction(
            task_id=task.id,
            task_ref=f"#{task.seq_num}",
            agent_slug="backend-developer",
            prompt="resume work",
        ),
    )

    def record_spawn(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append(action.task_id)
        return "0dc284d8-ee46-5ebb-961d-881bbee9b1d0"

    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert spawned == [task.id]
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id == "0dc284d8-ee46-5ebb-961d-881bbee9b1d0"


@pytest.mark.asyncio
async def test_heartbeat_preserves_fresh_no_run_mutex(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Heartbeat preserves fresh no run mutex."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=dispatcher.DISPATCH_TTL_SECONDS,
        now=datetime.now(UTC),
    )
    spawned: list[str] = []

    def record_spawn(action: SpawnAgentAction, **_kwargs: object) -> str:
        spawned.append(action.task_id)
        return "7747170d-ab96-5f8c-bade-08c58891d57d"

    monkeypatch.setattr(dispatcher, "spawn_agent", record_spawn)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 0
    assert spawned == []
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id is None


def test_run_heartbeat_serializes_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contended heartbeats on distinct event loops serialize instead of raising.

    The automation loop ticks on the daemon loop while the build route drives
    ticks via asyncio.run on a worker thread; a module-level asyncio.Lock bound
    itself to one loop on contended acquire and raised
    "is bound to a different event loop" for the other.
    """
    from gobby.dispatch import dispatcher

    class ObservedThreadLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.contention_observed = threading.Event()

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if self._lock.locked():
                self.contention_observed.set()
            if timeout == -1:
                acquired = self._lock.acquire(blocking=blocking)
            else:
                acquired = self._lock.acquire(blocking=blocking, timeout=timeout)
            return acquired

        def release(self) -> None:
            self._lock.release()

        def locked(self) -> bool:
            return self._lock.locked()

    state_lock = threading.Lock()
    heartbeat_lock = ObservedThreadLock()
    first_inside = threading.Event()
    release_heartbeat = threading.Event()
    active = 0
    max_active = 0

    async def fake_unlocked(**_kwargs: Any) -> Any:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        first_inside.set()
        await asyncio.to_thread(release_heartbeat.wait)
        with state_lock:
            active -= 1
        return dispatcher.HeartbeatResult()

    monkeypatch.setattr(dispatcher, "_HEARTBEAT_LOCK", heartbeat_lock)
    monkeypatch.setattr(dispatcher, "_run_heartbeat_unlocked", fake_unlocked)

    errors: list[Exception] = []

    def run_tick() -> None:
        try:
            asyncio.run(dispatcher.run_heartbeat(db=MagicMock()))
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run_tick, daemon=True)
    second = threading.Thread(target=run_tick, daemon=True)
    first.start()
    assert first_inside.wait(timeout=5.0)
    second.start()
    assert heartbeat_lock.contention_observed.wait(timeout=5.0)
    release_heartbeat.set()
    first.join(timeout=10.0)
    second.join(timeout=10.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert max_active == 1
    assert not dispatcher._HEARTBEAT_LOCK.locked()


@pytest.mark.asyncio
async def test_run_heartbeat_closes_owned_database_when_dispatch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    class DatabaseContext:
        def __init__(self) -> None:
            self.entered = False
            self.exited = False

        def __enter__(self) -> object:
            self.entered = True
            return object()

        def __exit__(
            self,
            _exc_type: object,
            _exc_value: object,
            _traceback: object,
        ) -> None:
            self.exited = True

    database_context = DatabaseContext()
    run_db_calls: list[str] = []

    async def run_inline(func: Any, *args: Any) -> Any:
        run_db_calls.append(func.__name__)
        return func(*args)

    async def fail_heartbeat(**_kwargs: Any) -> dispatcher.HeartbeatResult:
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database",
        lambda **_kwargs: database_context,
    )
    monkeypatch.setattr(dispatcher, "run_db", run_inline)
    monkeypatch.setattr(dispatcher, "_run_heartbeat_unlocked", fail_heartbeat)

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await dispatcher.run_heartbeat()

    assert run_db_calls == ["enter_context", "close"]
    assert database_context.entered is True
    assert database_context.exited is True


@pytest.mark.asyncio
async def test_unattached_spawn_cleanup_delivers_terminal_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import spawn_actions

    run = SimpleNamespace(id="run-1", status="running")
    storage = MagicMock()
    storage.get.return_value = run
    storage.fail.return_value = SimpleNamespace(id=run.id, status="error")
    completion_registry = object()
    delivered: list[tuple[str, object]] = []

    async def deliver_terminal_run(**kwargs: Any) -> bool:
        delivered.append((kwargs["run_id"], kwargs["completion_registry"]))
        return True

    async def kill_terminal_run(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        return {"success": True}

    monkeypatch.setattr(spawn_actions, "LocalAgentRunManager", lambda _db: storage)
    monkeypatch.setattr(
        "gobby.agents.kill.kill_agent",
        kill_terminal_run,
    )
    monkeypatch.setattr(
        spawn_actions,
        "deliver_existing_terminal_run_in_scope",
        deliver_terminal_run,
    )

    cleaned = await spawn_actions.cleanup_unattached_spawned_run(
        run.id,
        db=MagicMock(),
        error="attach failed",
        completion_registry=completion_registry,
    )

    assert cleaned is True
    assert delivered == [(run.id, completion_registry)]
