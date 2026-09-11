from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.ask.contracts import AskRequest, AskRunRecord, ProfileSnapshot
from gobby.ask.pipeline import parse_ask_pipeline
from gobby.ask.service import AskService
from gobby.ask.stages import AskStageStore
from gobby.ask.storage import AskRunStorage
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution, StepStatus

pytestmark = pytest.mark.unit


def _profile(identifier: str, _timeout: float) -> ProfileSnapshot:
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-09T12:00:00+00:00",
        effective={"name": identifier, "provider": "claude", "model": "claude-test"},
    )


def _pipeline_snapshot() -> dict[str, Any]:
    path = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    return parse_ask_pipeline(yaml.safe_load(path.read_text())).model_dump(mode="json")


class _DelayedPipelineExecutor:
    def __init__(self) -> None:
        self.cancelled = False

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> PipelineExecution:
        del pipeline, inputs, project_id, execution_id, session_id
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise RuntimeError("stalled pipeline escaped the Ask deadline")


class _CancellingPipelineExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaning_up = asyncio.Event()
        self.release_cleanup = asyncio.Event()

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> PipelineExecution:
        del pipeline, inputs, project_id, execution_id, session_id
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleaning_up.set()
            await self.release_cleanup.wait()
            raise
        raise AssertionError("cancelled executor unexpectedly completed")


class _RecordingPipelineExecutor:
    def __init__(self, manager: LocalPipelineExecutionManager) -> None:
        self.manager = manager
        self.calls: list[tuple[str | None, str | None, dict[str, Any]]] = []

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> PipelineExecution:
        del pipeline, project_id
        self.calls.append((execution_id, session_id, inputs))
        assert execution_id is not None
        execution = self.manager.update_execution_status(
            execution_id,
            ExecutionStatus.COMPLETED,
        )
        assert execution is not None
        return execution


class _NoopPermissions:
    def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
        del ask_run_id, reason
        return 0


class _NoopAgents:
    def preflight(self, _profiles: object) -> None:
        return None

    def status(self, agent_run_id: str) -> str | None:
        del agent_run_id
        return None

    async def cancel(self, agent_run_id: str) -> None:
        del agent_run_id


class _ObservedCompletionRegistry(CompletionEventRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()
        self.on_register: Callable[[], None] | None = None

    def register(
        self,
        completion_id: str,
        subscribers: list[str],
        continuation_prompt: str | None = None,
    ) -> bool:
        created = super().register(completion_id, subscribers, continuation_prompt)
        if self.on_register is not None:
            self.on_register()
        return created

    async def wait(self, completion_id: str, timeout: float | None = None) -> dict[str, Any]:
        self.waiting.set()
        return await super().wait(completion_id, timeout)


@pytest.fixture
def waiting_run(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> tuple[AskService, AskRunRecord, _ObservedCompletionRegistry]:
    manager = LocalPipelineExecutionManager(temp_db, project_id=str(sample_project["id"]))
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=_pipeline_snapshot(),
    )
    record = storage.start(
        AskRequest(
            question="Wait for the existing run",
            project_id=str(sample_project["id"]),
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    storage.bind_execution_context(
        record.run_id, project_root=tmp_path, caller_session_id="original-caller"
    )
    registry = _ObservedCompletionRegistry()
    service = AskService(
        storage=storage,
        stages=stages,
        snapshot_manager=cast(Any, object()),
        agents=cast(Any, _NoopAgents()),
        permissions=cast(Any, _NoopPermissions()),
        pipeline_executor=_RecordingPipelineExecutor(manager),
        state_root=tmp_path / "state",
        completion_registry=registry,
    )
    return service, record, registry


@pytest.mark.asyncio
async def test_wait_without_local_executor_times_out_without_changing_run(
    waiting_run: tuple[AskService, AskRunRecord, CompletionEventRegistry],
) -> None:
    service, record, _registry = waiting_run
    before = service.get(record.run_id, project_id=record.binding.project_id)
    with pytest.raises(TimeoutError, match="timed out waiting for Ask run"):
        await service.wait(record.run_id, project_id=record.binding.project_id, timeout=0.01)
    after = service.get(record.run_id, project_id=record.binding.project_id)
    assert after == before


@pytest.mark.asyncio
async def test_wait_ignores_previous_completion_after_resume_claim_before_enqueue(
    waiting_run: tuple[AskService, AskRunRecord, _ObservedCompletionRegistry],
) -> None:
    from gobby.ask.recovery import AskRecoveryController

    service, record, registry = waiting_run
    project_id = record.binding.project_id
    completion_id = f"ask:{record.run_id}"
    service.storage.manager.update_execution_status(record.run_id, ExecutionStatus.FAILED)
    registry.register(completion_id, subscribers=[])
    await registry.notify(completion_id, {"status": "failed"})
    recovery = AskRecoveryController(
        manager=service.storage.manager,
        stages=service.stages,
        permissions=service.permissions,
        agents=service.agents,
    )
    decision = recovery.inspect(record.run_id, project_id=project_id)
    recovery.claim_resume(
        record.run_id,
        project_id=project_id,
        caller_session_id="resume-operator",
        decision=decision,
    )

    with pytest.raises(TimeoutError, match="timed out waiting for Ask run"):
        await service.wait(record.run_id, project_id=project_id, timeout=0.05)

    assert service.get(record.run_id, project_id=project_id).status == "pending"
    assert await service.recover_daemon_execution(record.run_id, project_id=project_id)
    result = await service.wait(record.run_id, project_id=project_id, timeout=1)
    assert result.status == "completed"
    assert result.binding.deadline_at == record.binding.deadline_at


@pytest.mark.asyncio
async def test_wait_returns_cancelled_run_after_local_executor_cleanup(
    waiting_run: tuple[AskService, AskRunRecord, _ObservedCompletionRegistry],
) -> None:
    service, record, _registry = waiting_run
    executor = _CancellingPipelineExecutor()
    service.pipeline_executor = executor
    project_id = record.binding.project_id
    assert await service.recover_daemon_execution(record.run_id, project_id=project_id)
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    cancellation = asyncio.create_task(
        service.cancel(record.run_id, project_id=project_id, caller_session_id="operator")
    )
    waiter: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(executor.cleaning_up.wait(), timeout=1)
        waiter = asyncio.create_task(service.wait(record.run_id, project_id=project_id, timeout=1))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), timeout=0.01)
        executor.release_cleanup.set()
        cancelled = await asyncio.wait_for(cancellation, timeout=1)
        result = await waiter
        assert result.status == "cancelled"
        assert result == cancelled
    finally:
        executor.release_cleanup.set()
        await asyncio.gather(cancellation, return_exceptions=True)
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_wait_rechecks_durable_completion_after_registration(
    waiting_run: tuple[AskService, AskRunRecord, _ObservedCompletionRegistry],
) -> None:
    service, record, registry = waiting_run

    def complete_before_notification_can_be_received() -> None:
        service.storage.manager.update_execution_status(record.run_id, ExecutionStatus.COMPLETED)

    registry.on_register = complete_before_notification_can_be_received
    result = await service.wait(record.run_id, project_id=record.binding.project_id, timeout=1)
    assert result.status == "completed"
    assert not registry.waiting.is_set()
    assert result.binding.deadline_at == record.binding.deadline_at


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "disconnect"])
async def test_one_waiter_cannot_interrupt_another_or_the_remote_execution(
    waiting_run: tuple[AskService, AskRunRecord, _ObservedCompletionRegistry],
    interruption: str,
) -> None:
    service, record, registry = waiting_run
    project_id = record.binding.project_id
    surviving_waiter = asyncio.create_task(service.wait(record.run_id, project_id=project_id))
    try:
        await asyncio.wait_for(registry.waiting.wait(), timeout=1)
        registry.waiting.clear()
        if interruption == "timeout":
            with pytest.raises(TimeoutError):
                await service.wait(record.run_id, project_id=project_id, timeout=0.01)
        else:
            disconnected = asyncio.create_task(service.wait(record.run_id, project_id=project_id))
            await asyncio.wait_for(registry.waiting.wait(), timeout=1)
            disconnected.cancel()
            with pytest.raises(asyncio.CancelledError):
                await disconnected
        assert not surviving_waiter.done()
        assert service.get(record.run_id, project_id=project_id).status == "pending"
        owner = AskService(
            storage=service.storage,
            stages=service.stages,
            snapshot_manager=service.snapshot_manager,
            agents=service.agents,
            permissions=service.permissions,
            pipeline_executor=service.pipeline_executor,
            state_root=service.state_root,
            completion_registry=registry,
        )
        assert await owner.recover_daemon_execution(record.run_id, project_id=project_id)
        result = await asyncio.wait_for(surviving_waiter, timeout=1)
        assert result.status == "completed"
        assert result.run_id == record.run_id
        assert result.binding.deadline_at == record.binding.deadline_at
        assert await service.wait(record.run_id, project_id=project_id, timeout=1) == result
    finally:
        surviving_waiter.cancel()
        await asyncio.gather(surviving_waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_pipeline_notification_does_not_finish_wait_before_ask_cleanup(
    waiting_run: tuple[AskService, AskRunRecord, _ObservedCompletionRegistry],
) -> None:
    service, record, registry = waiting_run
    waiter = asyncio.create_task(service.wait(record.run_id, project_id=record.binding.project_id))
    try:
        await asyncio.wait_for(registry.waiting.wait(), timeout=1)
        # The executor publishes its event before Ask maps errors and releases resources.
        registry.register(record.run_id, subscribers=[])
        await registry.notify(record.run_id, {"status": "failed"})
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), timeout=0.01)
        await service.cancel(
            record.run_id,
            project_id=record.binding.project_id,
            caller_session_id="original-caller",
        )
        result = await asyncio.wait_for(waiter, timeout=1)
        assert result.status == "cancelled"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def test_failed_resume_claim_is_atomic_and_preserves_stage_outputs(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.recovery import AskRecoveryController
    from gobby.ask.stages import AskStage, AskStageStore

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    record = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
    ).start(
        AskRequest(
            question="Which checkpoints survive recovery?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    stages.checkpoint(
        record.run_id,
        stage=AskStage.INVESTIGATOR,
        boundary_id="investigator:0:interrupted",
    )
    prepare = manager.create_step_execution(record.run_id, "prepare")
    investigate = manager.create_step_execution(record.run_id, "investigate")
    manager.update_step_execution(
        prepare.id,
        status=StepStatus.COMPLETED,
        output_json=json.dumps({"checkpoint": "prepared"}),
    )
    manager.update_step_execution(
        investigate.id,
        status=StepStatus.FAILED,
        output_json=json.dumps({"checkpoint": "interrupted"}),
        error="simulated process boundary",
    )
    manager.update_execution_status(record.run_id, ExecutionStatus.FAILED)
    outputs_before = {
        step.step_id: step.output_json for step in manager.get_steps_for_execution(record.run_id)
    }

    class Permissions:
        def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
            raise AssertionError(f"resume revoked {ask_run_id}: {reason}")

    class Agents:
        def status(self, agent_run_id: str) -> str | None:
            return None

        async def cancel(self, agent_run_id: str) -> None:
            raise AssertionError(f"resume cancelled {agent_run_id}")

    controller = AskRecoveryController(
        manager=manager,
        stages=stages,
        permissions=Permissions(),
        agents=Agents(),
    )
    first = controller.inspect(record.run_id, project_id=project_id)
    stale = controller.inspect(record.run_id, project_id=project_id)
    claimed = controller.claim_resume(
        record.run_id,
        project_id=project_id,
        caller_session_id="resume-operator",
        decision=first,
    )

    assert claimed.status == ExecutionStatus.PENDING.value
    execution = manager.get_execution(record.run_id)
    assert execution is not None
    assert execution.status is ExecutionStatus.PENDING
    resumed_steps = manager.get_steps_for_execution(record.run_id)
    assert [step.status for step in resumed_steps] == [StepStatus.COMPLETED, StepStatus.PENDING]
    assert {step.step_id: step.output_json for step in resumed_steps} == outputs_before
    with pytest.raises(ValueError, match="already being resumed"):
        controller.claim_resume(
            record.run_id,
            project_id=project_id,
            caller_session_id="other-operator",
            decision=stale,
        )

    manager.update_execution_status(record.run_id, ExecutionStatus.INTERRUPTED)
    recovered = controller.inspect(record.run_id, project_id=project_id)
    controller.claim_resume(
        record.run_id,
        project_id=project_id,
        caller_session_id="resume-operator",
        decision=recovered,
    )
    reclaimed_execution = manager.get_execution(record.run_id)
    assert reclaimed_execution is not None
    assert reclaimed_execution.status is ExecutionStatus.PENDING


@pytest.mark.asyncio
async def test_pipeline_executor_is_cancelled_at_the_immutable_deadline(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.service import AskService

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=_pipeline_snapshot(),
    )
    executor = _DelayedPipelineExecutor()
    service = AskService(
        storage=storage,
        stages=AskStageStore(manager),
        snapshot_manager=cast(Any, object()),
        agents=cast(Any, _NoopAgents()),
        permissions=cast(Any, _NoopPermissions()),
        pipeline_executor=executor,
        state_root=tmp_path / "state",
    )

    started = await service.start(
        AskRequest(
            question="Does the executor honor the absolute deadline?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
            timeout_seconds=0.05,
        ),
        project_root=tmp_path,
        caller_session_id="caller-session",
    )
    result = await service.wait(started.run_id, project_id=project_id, timeout=1)

    assert result.status == ExecutionStatus.FAILED.value
    assert result.typed_error is not None
    assert result.typed_error["code"] == "deadline_exceeded"
    assert executor.cancelled is True


@pytest.mark.asyncio
async def test_resume_validates_immutable_context_and_definition_before_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.service import AskService

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=_pipeline_snapshot(),
    )
    record = storage.start(
        AskRequest(
            question="Can malformed recovery state reserve the run?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    original_inputs = storage.bind_execution_context(
        record.run_id,
        project_root=tmp_path,
        caller_session_id="original-caller",
    )
    first_step = manager.create_step_execution(record.run_id, "prepare")
    manager.update_step_execution(first_step.id, status=StepStatus.FAILED)
    manager.update_execution_status(record.run_id, ExecutionStatus.FAILED)
    service = AskService(
        storage=storage,
        stages=stages,
        snapshot_manager=cast(Any, object()),
        agents=cast(Any, _NoopAgents()),
        permissions=cast(Any, _NoopPermissions()),
        pipeline_executor=_DelayedPipelineExecutor(),
        state_root=tmp_path / "state",
    )

    malformed_inputs = json.loads(json.dumps(original_inputs))
    malformed_inputs["caller_session_id"] = ""
    ask_inputs = cast(dict[str, Any], malformed_inputs["ask"])
    execution_context = cast(dict[str, Any], ask_inputs["execution_context"])
    execution_context["caller_session_id"] = ""
    with temp_db.transaction() as connection:
        connection.execute(
            "UPDATE pipeline_executions SET inputs_json = %s WHERE id = %s",
            (json.dumps(malformed_inputs), record.run_id),
        )

    with pytest.raises(RuntimeError, match="immutable caller session"):
        await service.resume(
            record.run_id,
            project_id=project_id,
            caller_session_id="resume-operator",
        )
    after_context_failure = manager.get_execution(record.run_id)
    assert after_context_failure is not None
    assert after_context_failure.status is ExecutionStatus.FAILED

    with temp_db.transaction() as connection:
        connection.execute(
            """
            UPDATE pipeline_executions
            SET inputs_json = %s, definition_json = %s
            WHERE id = %s
            """,
            (json.dumps(original_inputs), json.dumps({"name": "native-ask"}), record.run_id),
        )
    with pytest.raises(ValueError):
        await service.resume(
            record.run_id,
            project_id=project_id,
            caller_session_id="resume-operator",
        )
    after_definition_failure = manager.get_execution(record.run_id)
    assert after_definition_failure is not None
    assert after_definition_failure.status is ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_daemon_recovery_adopts_pending_start_before_enqueue(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.service import AskService

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=_pipeline_snapshot(),
    )
    record = storage.start(
        AskRequest(
            question="Can startup adopt a reserved Ask run?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    storage.bind_execution_context(
        record.run_id,
        project_root=tmp_path,
        caller_session_id="original-caller",
    )
    executor = _RecordingPipelineExecutor(manager)
    service = AskService(
        storage=storage,
        stages=stages,
        snapshot_manager=cast(Any, object()),
        agents=cast(Any, _NoopAgents()),
        permissions=cast(Any, _NoopPermissions()),
        pipeline_executor=executor,
        state_root=tmp_path / "state",
    )

    claims = await asyncio.gather(
        service.recover_daemon_execution(record.run_id, project_id=project_id),
        service.recover_daemon_execution(record.run_id, project_id=project_id),
    )
    await service.wait(record.run_id, project_id=project_id, timeout=1)

    assert sorted(claims) == [False, True]
    assert [(call[0], call[1]) for call in executor.calls] == [(record.run_id, "original-caller")]
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM pipeline_executions WHERE id = %s",
        (record.run_id,),
    )
    assert row is not None and row["count"] == 1


@pytest.mark.asyncio
async def test_daemon_recovery_adopts_pending_resume_claim_before_enqueue(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.recovery import AskRecoveryController
    from gobby.ask.service import AskService

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=_pipeline_snapshot(),
    )
    record = storage.start(
        AskRequest(
            question="Can startup adopt an already claimed resume?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    storage.bind_execution_context(
        record.run_id,
        project_root=tmp_path,
        caller_session_id="original-caller",
    )
    manager.update_execution_status(record.run_id, ExecutionStatus.FAILED)
    recovery = AskRecoveryController(
        manager=manager,
        stages=stages,
        permissions=_NoopPermissions(),
        agents=_NoopAgents(),
    )
    decision = recovery.inspect(record.run_id, project_id=project_id)
    recovery.claim_resume(
        record.run_id,
        project_id=project_id,
        caller_session_id="resume-operator",
        decision=decision,
    )
    executor = _RecordingPipelineExecutor(manager)
    service = AskService(
        storage=storage,
        stages=stages,
        snapshot_manager=cast(Any, object()),
        agents=cast(Any, _NoopAgents()),
        permissions=cast(Any, _NoopPermissions()),
        pipeline_executor=executor,
        state_root=tmp_path / "state",
    )

    assert await service.recover_daemon_execution(record.run_id, project_id=project_id)
    await service.wait(record.run_id, project_id=project_id, timeout=1)

    assert [(call[0], call[1]) for call in executor.calls] == [(record.run_id, "original-caller")]
    execution = manager.get_execution(record.run_id)
    assert execution is not None
    inputs = json.loads(execution.inputs_json or "{}")
    assert inputs["ask"]["runtime"]["resume_claim"]["caller_session_id"] == "resume-operator"


@pytest.mark.asyncio
async def test_daemon_recovery_bounds_running_run_by_original_deadline(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.service import AskService

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=_pipeline_snapshot(),
        now=lambda: datetime.now(UTC) - timedelta(minutes=5),
    )
    record = storage.start(
        AskRequest(
            question="Can a restarted run outlive its deadline?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
            timeout_seconds=1,
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    storage.bind_execution_context(
        record.run_id,
        project_root=tmp_path,
        caller_session_id="original-caller",
    )
    manager.update_execution_status(record.run_id, ExecutionStatus.RUNNING)
    executor = _RecordingPipelineExecutor(manager)
    service = AskService(
        storage=storage,
        stages=stages,
        snapshot_manager=cast(Any, object()),
        agents=cast(Any, _NoopAgents()),
        permissions=cast(Any, _NoopPermissions()),
        pipeline_executor=executor,
        state_root=tmp_path / "state",
    )

    assert await service.recover_daemon_execution(record.run_id, project_id=project_id)
    result = await service.wait(record.run_id, project_id=project_id, timeout=1)

    assert executor.calls == []
    assert result.status == ExecutionStatus.FAILED.value
    assert result.typed_error is not None
    assert result.typed_error["code"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_deadline_and_stage_boundary_recovery(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    try:
        from gobby.ask.recovery import AskRecoveryController
        from gobby.ask.stages import AskErrorCode, AskStage, AskStageStore
    except ModuleNotFoundError:
        raise NotImplementedError("native Ask recovery is not implemented") from None

    project_id = str(sample_project["id"])
    admitted_at = datetime.now(UTC)
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        now=lambda: admitted_at,
    )
    record = storage.start(
        AskRequest(
            question="Can recovery duplicate an Ask attempt?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
            timeout_seconds=120,
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    state = stages.initialize(record)
    original_deadline = state.deadline_at
    attempt = stages.reserve_attempt(
        record.run_id,
        stage=AskStage.INVESTIGATOR,
        attempt=0,
        boundary_id="investigator:0:reserved",
    )
    bound = stages.bind_agent(
        record.run_id,
        stage=AskStage.INVESTIGATOR,
        attempt=0,
        agent_run_id="agent-original",
        boundary_id="investigator:0:launched",
    )

    recovered = AskStageStore(manager).get(record.run_id)
    assert recovered is not None
    assert recovered.deadline_at == original_deadline == admitted_at + timedelta(seconds=120)
    assert recovered.attempts == (bound,)
    assert bound.status.value == "running"
    assert bound.agent_run_id == "agent-original"
    assert bound.attempt == attempt.attempt
    duplicate = stages.reserve_attempt(
        record.run_id,
        stage=AskStage.INVESTIGATOR,
        attempt=0,
        boundary_id="investigator:0:reserved",
    )
    assert duplicate.agent_run_id == "agent-original"
    durable = stages.get(record.run_id)
    assert durable is not None
    assert len(durable.attempts) == 1

    events: list[str] = []

    class Permissions:
        def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
            events.append(f"revoke:{ask_run_id}:{reason}")
            return 1

    class Agents:
        def status(self, agent_run_id: str) -> str | None:
            assert agent_run_id == "agent-original"
            return "running"

        async def cancel(self, agent_run_id: str) -> None:
            events.append(f"cleanup:{agent_run_id}")

    controller = AskRecoveryController(
        manager=manager,
        stages=stages,
        permissions=Permissions(),
        agents=Agents(),
    )
    decision = controller.inspect(record.run_id, project_id=project_id)
    assert decision.reattach_agent_run_id == "agent-original"
    assert decision.restart_attempt is None

    waiter = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert controller.inspect(record.run_id, project_id=project_id).reattach_agent_run_id == (
        "agent-original"
    )

    cancelled = await controller.cancel(
        record.run_id,
        project_id=project_id,
        caller_session_id="caller-session",
    )
    assert events == [
        f"revoke:{record.run_id}:cancelled",
        "cleanup:agent-original",
    ]
    assert cancelled.status == "cancelled"

    expired_record = storage.start(
        AskRequest(
            question="Is the deadline reset?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
            timeout_seconds=1,
        ),
        tmp_path,
    )
    stages.initialize(expired_record)
    with pytest.raises(TimeoutError, match=AskErrorCode.DEADLINE_EXCEEDED.value):
        controller.inspect(
            expired_record.run_id,
            project_id=project_id,
            now=expired_record.binding.deadline_at,
        )


def test_identical_stage_submission_replay_is_idempotent(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.permissions import AskAgentStage
    from gobby.ask.stages import AskStageStore

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
    )
    record = storage.start(
        AskRequest(
            question="Can a completed submission be replayed safely?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    stages.reserve_attempt(
        record.run_id,
        stage=AskAgentStage.INVESTIGATOR,
        attempt=0,
        boundary_id="investigator:0:reserved",
    )
    stages.bind_agent(
        record.run_id,
        stage=AskAgentStage.INVESTIGATOR,
        attempt=0,
        agent_run_id="agent-original",
        boundary_id="investigator:0:launched",
    )
    first = stages.record_submission(
        record.run_id,
        stage=AskAgentStage.INVESTIGATOR,
        attempt=0,
        agent_run_id="agent-original",
        artifact={"kind": "answer-draft", "sha256": "a" * 64},
        submission_hash="b" * 64,
        evidence_manifest_hash="c" * 64,
        boundary_id="investigator:0:answer:" + "b" * 64,
    )
    replay = stages.record_submission(
        record.run_id,
        stage=AskAgentStage.INVESTIGATOR,
        attempt=0,
        agent_run_id="agent-original",
        artifact={"kind": "answer-draft", "sha256": "a" * 64},
        submission_hash="b" * 64,
        evidence_manifest_hash="c" * 64,
        boundary_id="investigator:0:answer:" + "b" * 64,
    )

    assert replay == first
    state = stages.get(record.run_id)
    assert state is not None
    assert state.attempts == (first,)
