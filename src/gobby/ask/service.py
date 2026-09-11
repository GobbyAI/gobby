"""Native Ask orchestration over immutable snapshots and managed agents."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from gobby.ask.agents import AskAgentRuntime
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.claims import AnswerDraft, ReviewerResult, canonical_hash
from gobby.ask.contracts import (
    AskRequest,
    AskRunRecord,
    AskRunResult,
)
from gobby.ask.errors import AskLifecycleConflict, AskRunNotFound
from gobby.ask.evidence_runtime import (
    AskSnapshotManager,
    EvidenceFactory,
    EvidenceManifestFactory,
    evidence_references,
)
from gobby.ask.permissions import AskAgentStage, AskPermissionRuntime, UnsupportedAskRuntime
from gobby.ask.pipeline import AskPipelineExecutor, parse_ask_pipeline
from gobby.ask.publication import PublicationError, replay_publication
from gobby.ask.recovery import AskRecoveryController
from gobby.ask.snapshots import SnapshotDriftError
from gobby.ask.stage_authority import require_ask_stage_authority
from gobby.ask.stage_runtime import AskStageRuntime
from gobby.ask.stages import (
    AskErrorCode,
    AskOrchestrationState,
    AskStageStore,
    AskTypedError,
)
from gobby.ask.storage import AskRunStorage
from gobby.events.completion_registry import CompletionEventRegistry, CompletionResultEvictedError
from gobby.utils.session_context import get_current_agent_run_id
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.pipeline_state import ExecutionStatus

logger = logging.getLogger(__name__)

_REVIEW_RESERVE_SECONDS = 60.0


FaultInjector = Callable[[str], None]


class AskFaultInjected(RuntimeError):
    """Signal a simulated process interruption after a durable boundary."""

    def __init__(self, boundary_id: str) -> None:
        super().__init__(f"Ask fault injected after {boundary_id}")
        self.boundary_id = boundary_id


class AskService:
    """Own one durable native Ask state machine and its in-process continuations."""

    def __init__(
        self,
        *,
        storage: AskRunStorage,
        stages: AskStageStore,
        snapshot_manager: AskSnapshotManager,
        agents: AskAgentRuntime,
        permissions: AskPermissionRuntime,
        pipeline_executor: AskPipelineExecutor,
        state_root: Path | None,
        completion_registry: CompletionEventRegistry | None = None,
        evidence_factory: EvidenceFactory | None = None,
        evidence_manifest_factory: EvidenceManifestFactory | None = None,
        fault_injector: FaultInjector | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.stages = stages
        self.snapshot_manager = snapshot_manager
        self.agents = agents
        self.permissions = permissions
        self.pipeline_executor = pipeline_executor
        self.completion_registry = completion_registry or CompletionEventRegistry()
        self._configured_pipeline = parse_ask_pipeline(storage.pipeline_snapshot)
        self.state_root = state_root
        self.fault_injector = fault_injector
        self.now = now or (lambda: datetime.now(UTC))
        self._restart_owner_id = str(uuid4())
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.stage_runtime = AskStageRuntime(
            storage=storage,
            stages=stages,
            snapshot_manager=snapshot_manager,
            agents=agents,
            permissions=permissions,
            state_root=state_root,
            evidence_factory=evidence_factory,
            evidence_manifest_factory=evidence_manifest_factory,
            remaining_seconds=self._remaining,
            agent_deadline=self._agent_deadline,
            repair_time_remains=self._repair_time_remains,
            fault_injector=self._fault,
        )
        self.evidence = self.stage_runtime.evidence

    async def start(
        self,
        request: AskRequest,
        *,
        project_root: Path,
        caller_session_id: str,
    ) -> AskRunResult:
        record = await asyncio.to_thread(self.storage.start, request, project_root)
        await asyncio.to_thread(self.stages.initialize, record)
        inputs = await asyncio.to_thread(
            self.storage.bind_execution_context,
            record.run_id,
            project_root=project_root,
            caller_session_id=caller_session_id,
        )
        self._ensure_task(record, self._pipeline(record.run_id), inputs, caller_session_id)
        return await asyncio.to_thread(self._result, record)

    def get(self, run_id: str, *, project_id: str) -> AskRunResult:
        record = self._record(run_id, project_id)
        return self._result(record)

    async def wait(
        self,
        run_id: str,
        *,
        project_id: str,
        timeout: float | None = None,
    ) -> AskRunResult:
        record = await asyncio.to_thread(self._record, run_id, project_id)
        completion_id = f"ask:{run_id}"
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self.completion_registry.register(completion_id, subscribers=[])
                    result = await asyncio.to_thread(self._result, record)
                    if result.status in {"completed", "failed", "cancelled"}:
                        task = self._tasks.get(run_id)
                        if task is not None and not task.done():
                            # Observe cleanup without propagating execution cancellation
                            # to this caller or caller cancellation to the execution.
                            await asyncio.wait({task})
                            continue
                        await self.completion_registry.notify_and_cleanup(
                            completion_id,
                            {"status": result.status},
                        )
                        return result
                    if self.completion_registry.get_result(completion_id) is not None:
                        # A resume claim can precede replacement of the old notification.
                        self.completion_registry.cleanup(completion_id)
                        continue
                    try:
                        # A caller timeout must not clean up other waiters' subscription.
                        await self.completion_registry.wait(completion_id)
                    except (CompletionResultEvictedError, KeyError):
                        # A resumed execution may replace a completed registration.
                        continue
        except TimeoutError:
            raise TimeoutError(f"timed out waiting for Ask run {run_id}") from None

    async def resume(
        self,
        run_id: str,
        *,
        project_id: str,
        caller_session_id: str,
    ) -> AskRunResult:
        record = self._record(run_id, project_id)
        recovery = AskRecoveryController(
            manager=self.storage.manager,
            stages=self.stages,
            permissions=self.permissions,
            agents=self.agents,
        )
        inputs = await asyncio.to_thread(self.storage.execution_inputs, run_id)
        original_caller = inputs.get("caller_session_id")
        if not isinstance(original_caller, str) or not original_caller:
            raise RuntimeError("Ask execution context has no immutable caller session")
        pipeline = self._pipeline(run_id)
        await self._drain_task_before_resume(record)
        decision = await asyncio.to_thread(recovery.inspect, run_id, project_id=project_id)
        claim_task = asyncio.create_task(
            asyncio.to_thread(
                recovery.claim_resume,
                run_id,
                project_id=project_id,
                caller_session_id=caller_session_id,
                decision=decision,
            )
        )
        caller_cancelled = False
        while True:
            try:
                await asyncio.shield(claim_task)
                break
            except asyncio.CancelledError:
                if claim_task.cancelled():
                    raise
                caller_cancelled = True
        claim_task.result()
        self._ensure_task(record, pipeline, inputs, original_caller)
        if caller_cancelled:
            raise asyncio.CancelledError
        return await asyncio.to_thread(self._result, record)

    async def recover_daemon_execution(self, run_id: str, *, project_id: str) -> bool:
        """Adopt one native Ask run left pending or running by a prior daemon."""
        record = self._record(run_id, project_id)
        inputs = await asyncio.to_thread(self.storage.execution_inputs, run_id)
        original_caller = inputs.get("caller_session_id")
        if not isinstance(original_caller, str) or not original_caller:
            raise RuntimeError("Ask execution context has no immutable caller session")
        pipeline = self._pipeline(run_id)
        claimed = await asyncio.to_thread(
            self.storage.claim_restart,
            run_id,
            project_id=project_id,
            owner_id=self._restart_owner_id,
        )
        if not claimed:
            return False
        self._ensure_task(record, pipeline, inputs, original_caller)
        return True

    async def cancel(
        self,
        run_id: str,
        *,
        project_id: str,
        caller_session_id: str,
    ) -> AskRunResult:
        record = self._record(run_id, project_id)
        recovery = AskRecoveryController(
            manager=self.storage.manager,
            stages=self.stages,
            permissions=self.permissions,
            agents=self.agents,
        )
        await recovery.cancel(
            run_id,
            project_id=project_id,
            caller_session_id=caller_session_id,
        )
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._notify_waiters(record)
        return await asyncio.to_thread(self._result, record)

    async def query_evidence(
        self,
        *,
        run_id: str,
        operation: str,
        selector: Mapping[str, Any],
        continuation: str | None = None,
    ) -> dict[str, Any]:
        principal = self._authorize_current(
            "query_evidence",
            {"run_id": run_id, "operation": operation},
        )
        if principal.stage is AskAgentStage.REVIEWER:
            raise PermissionError("Ask reviewer cannot issue evidence queries")
        return await self.evidence.query(
            run_id=run_id,
            stage=principal.stage,
            attempt=principal.attempt,
            agent_run_id=principal.agent_run_id,
            operation=operation,
            selector=selector,
            continuation=continuation,
        )

    async def read_evidence(self, *, run_id: str, evidence_id: str) -> dict[str, Any]:
        self._authorize_current("read_evidence", {"run_id": run_id})
        evidence = await self.evidence.load_current(run_id)
        for recorded in evidence.records:
            for item in recorded.response.items:
                if item.evidence_id == evidence_id:
                    return item.model_dump(mode="json", by_alias=True)
        raise ValueError(f"Ask evidence not found: {evidence_id}")

    async def submit_answer(
        self,
        *,
        run_id: str,
        attempt: int,
        draft: Mapping[str, Any],
        draft_hash: str,
        evidence_manifest_hash: str,
    ) -> dict[str, Any]:
        principal = self._authorize_current(
            "submit_answer",
            {"run_id": run_id, "attempt": attempt},
        )
        answer = AnswerDraft.model_validate(draft)
        if (
            answer.run_id != run_id
            or answer.investigator_run_id != principal.agent_run_id
            or answer.content_hash != draft_hash
        ):
            raise ValueError("Ask answer submission identity or hash mismatch")
        async with self.evidence.submission_guard(run_id):
            state = self._required_state(run_id)
            if state.evidence_manifest_hash != evidence_manifest_hash:
                raise ValueError("Ask answer submission evidence hash is stale")
            record = self._record(run_id, principal.project_id)
            artifact = await asyncio.to_thread(
                self._artifacts(record).write_body,
                "answer-draft",
                answer.model_dump(mode="json"),
                timeout_seconds=self._remaining(state.deadline_at),
            )
            checkpoint = await asyncio.to_thread(
                self.stages.record_submission,
                run_id,
                stage=principal.stage,
                attempt=attempt,
                agent_run_id=principal.agent_run_id,
                artifact=artifact,
                submission_hash=draft_hash,
                evidence_manifest_hash=evidence_manifest_hash,
                boundary_id=f"{principal.stage.value}:{attempt}:answer:{draft_hash}",
            )
        return {"accepted": True, "submission_hash": checkpoint.submission_hash}

    async def submit_review(
        self,
        *,
        run_id: str,
        attempt: int,
        review: Mapping[str, Any],
        review_hash: str,
        draft_hash: str,
        evidence_manifest_hash: str,
    ) -> dict[str, Any]:
        principal = self._authorize_current(
            "submit_review",
            {"run_id": run_id, "attempt": attempt},
        )
        reviewer = ReviewerResult.model_validate(review)
        actual_hash = canonical_hash(reviewer.model_dump(mode="json"))
        if (
            principal.stage is not AskAgentStage.REVIEWER
            or reviewer.run_id != run_id
            or reviewer.reviewer_run_id != principal.agent_run_id
            or reviewer.draft_hash != draft_hash
            or reviewer.evidence_manifest_hash != evidence_manifest_hash
            or actual_hash != review_hash
        ):
            raise ValueError("Ask review submission identity or hash mismatch")
        async with self.evidence.submission_guard(run_id):
            state = self._required_state(run_id)
            if state.evidence_manifest_hash != evidence_manifest_hash:
                raise ValueError("Ask review submission evidence hash is stale")
            record = self._record(run_id, principal.project_id)
            artifact = await asyncio.to_thread(
                self._artifacts(record).write_body,
                "reviewer-result",
                reviewer.model_dump(mode="json"),
                timeout_seconds=self._remaining(state.deadline_at),
            )
            checkpoint = await asyncio.to_thread(
                self.stages.record_submission,
                run_id,
                stage=AskAgentStage.REVIEWER,
                attempt=attempt,
                agent_run_id=principal.agent_run_id,
                artifact=artifact,
                submission_hash=review_hash,
                evidence_manifest_hash=evidence_manifest_hash,
                boundary_id=f"reviewer:{attempt}:review:{review_hash}",
            )
        return {"accepted": True, "submission_hash": checkpoint.submission_hash}

    def _ensure_task(
        self,
        record: AskRunRecord,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        caller_session_id: str,
    ) -> None:
        existing = self._tasks.get(record.run_id)
        if existing is not None and not existing.done():
            return
        completion_id = f"ask:{record.run_id}"
        if self.completion_registry.get_result(completion_id) is not None:
            self.completion_registry.cleanup(completion_id)
        self.completion_registry.register(completion_id, subscribers=[])
        task = asyncio.create_task(
            self._execute_pipeline(record, pipeline, inputs, caller_session_id),
            name=f"native-ask:{record.run_id}",
        )
        self._tasks[record.run_id] = task
        task.add_done_callback(lambda completed: self._task_done(record.run_id, completed))

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("native Ask background task escaped its boundary", exc_info=error)

    async def _drain_task_before_resume(self, record: AskRunRecord) -> None:
        previous = self._tasks.get(record.run_id)
        if previous is None or previous.done():
            return
        try:
            async with asyncio.timeout(self._remaining(record.binding.deadline_at)):
                await asyncio.shield(previous)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            if not previous.done():
                raise

    async def _execute_pipeline(
        self,
        record: AskRunRecord,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        caller_session_id: str,
    ) -> None:
        release_resources = True
        try:
            async with asyncio.timeout(self._remaining(record.binding.deadline_at)):
                await asyncio.to_thread(
                    self.agents.preflight,
                    {
                        AskAgentStage.INVESTIGATOR: record.investigator,
                        AskAgentStage.REVIEWER: record.reviewer,
                    },
                )
                await self.pipeline_executor.execute(
                    pipeline,
                    inputs,
                    record.binding.project_id,
                    execution_id=record.run_id,
                    session_id=caller_session_id,
                )
        except asyncio.CancelledError:
            await self.stage_runtime.wait_for_publication_cleanup(record.run_id)
            raise
        except AskFaultInjected as error:
            release_resources = False
            await self._fail(record, AskErrorCode.AGENT_FAILED, str(error), cleanup=False)
        except TimeoutError as error:
            await self._fail(record, AskErrorCode.DEADLINE_EXCEEDED, str(error))
        except SnapshotDriftError as error:
            await self._fail(record, AskErrorCode.SNAPSHOT_MISMATCH, str(error))
        except (PublicationError, UnsupportedAskRuntime, ValueError) as error:
            await self._fail(record, AskErrorCode.VALIDATION_FAILED, str(error))
        except Exception as error:
            await self._fail(record, AskErrorCode.AGENT_FAILED, str(error))
        finally:
            try:
                if release_resources:
                    await self.stage_runtime.release(record.run_id)
            finally:
                await self._notify_waiters(record)

    async def _notify_waiters(self, record: AskRunRecord) -> None:
        result = await asyncio.to_thread(self._result, record)
        if result.status in {"completed", "failed", "cancelled"}:
            # Pipeline completion precedes Ask error mapping and resource cleanup.
            await self.completion_registry.notify_and_cleanup(
                f"ask:{record.run_id}",
                {"status": result.status},
            )

    async def prepare(
        self,
        *,
        run_id: str,
        project_id: str,
        project_root: Path,
    ) -> dict[str, Any]:
        self._authorize_stage("prepare", run_id=run_id, project_id=project_id)
        return await self.stage_runtime.prepare(
            self._record(run_id, project_id),
            project_root=project_root,
        )

    async def seed(self, *, run_id: str, project_id: str) -> dict[str, Any]:
        self._authorize_stage("seed", run_id=run_id, project_id=project_id)
        return await self.stage_runtime.seed(self._record(run_id, project_id))

    async def spawn(
        self,
        *,
        run_id: str,
        project_id: str,
        stage: AskAgentStage,
        attempt: int,
        caller_session_id: str,
    ) -> dict[str, Any]:
        self._authorize_stage(
            "spawn",
            run_id=run_id,
            project_id=project_id,
            stage=stage,
            attempt=attempt,
        )
        return await self.stage_runtime.spawn(
            self._record(run_id, project_id),
            stage=stage,
            attempt=attempt,
            caller_session_id=caller_session_id,
        )

    async def validate(
        self,
        *,
        run_id: str,
        project_id: str,
        stage: AskAgentStage,
        attempt: int,
    ) -> dict[str, Any]:
        self._authorize_stage(
            "validate",
            run_id=run_id,
            project_id=project_id,
            stage=stage,
            attempt=attempt,
        )
        return await self.stage_runtime.validate(
            self._record(run_id, project_id),
            stage=stage,
            attempt=attempt,
        )

    async def admit_repair(self, *, run_id: str, project_id: str) -> dict[str, Any]:
        self._authorize_stage("admit_repair", run_id=run_id, project_id=project_id)
        return await self.stage_runtime.admit_repair(self._record(run_id, project_id))

    async def publish(self, *, run_id: str, project_id: str) -> dict[str, Any]:
        self._authorize_stage("publish", run_id=run_id, project_id=project_id)
        return await self.stage_runtime.publish(self._record(run_id, project_id))

    def _authorize_stage(
        self,
        operation: str,
        *,
        run_id: str,
        project_id: str,
        stage: AskAgentStage | None = None,
        attempt: int | None = None,
    ) -> None:
        require_ask_stage_authority(
            self.storage.manager,
            operation,
            run_id=run_id,
            project_id=project_id,
            stage=stage,
            attempt=attempt,
        )

    def publication_root(self, run_id: str, *, project_id: str) -> Path:
        """Return a verified canonical publication directory for a completed run."""
        self._record(run_id, project_id)
        state = self._required_state(run_id)
        if state.status != ExecutionStatus.COMPLETED.value or state.publication is None:
            raise AskLifecycleConflict("Ask run has no completed publication")
        root_value = state.publication.get("root")
        expected_hash = state.publication.get("manifest_sha256")
        if not isinstance(root_value, str) or not isinstance(expected_hash, str):
            raise PublicationError("Ask publication checkpoint is invalid")
        root = Path(root_value)
        replay = replay_publication(root)
        if replay.manifest_sha256 != expected_hash:
            raise PublicationError("Ask publication checkpoint hash does not match stored bytes")
        return root

    async def _fail(
        self,
        record: AskRunRecord,
        code: AskErrorCode,
        message: str,
        *,
        cleanup: bool = True,
    ) -> None:
        state = self.stages.get(record.run_id)
        if state is None or state.status in {"completed", "cancelled"}:
            return
        if cleanup:
            self.permissions.revoke_for_run(record.run_id, reason=code.value)
            for attempt in state.attempts:
                if attempt.agent_run_id and self.agents.status(attempt.agent_run_id) in {
                    "pending",
                    "running",
                }:
                    await self.agents.cancel(attempt.agent_run_id)
        await asyncio.to_thread(
            self.stages.terminate,
            record.run_id,
            status=ExecutionStatus.FAILED,
            stage=state.current_stage,
            boundary_id=f"failed:{code.value}",
            error=AskTypedError(code=code, message=message or code.value),
            answer_outcome=state.answer_outcome,
            publication=state.publication,
        )

    def _authorize_current(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        agent_run_id = get_current_agent_run_id()
        if agent_run_id is None:
            raise PermissionError("Ask agent authentication is required")
        return self.permissions.authorize(
            agent_run_id,
            "gobby-ask",
            tool_name,
            arguments,
        )

    def _record(self, run_id: str, project_id: str) -> AskRunRecord:
        record = self.storage.get(run_id)
        if record is None or record.binding.project_id != project_id:
            raise AskRunNotFound(f"Ask run not found: {run_id}")
        return record

    def _result(self, record: AskRunRecord) -> AskRunResult:
        return self.stages.to_result(
            record,
            evidence=tuple(evidence_references(self.storage, record.run_id)),
        )

    def _required_state(self, run_id: str) -> AskOrchestrationState:
        state = self.stages.get(run_id)
        if state is None:
            raise RuntimeError(f"Ask orchestration state not found: {run_id}")
        return state

    def _artifacts(self, record: AskRunRecord) -> AskArtifactStore:
        return AskArtifactStore(self.state_root, record.binding.project_id, record.run_id)

    def _remaining(self, deadline_at: datetime) -> float:
        current = self.now()
        if current.tzinfo is None:
            raise ValueError("Ask clock must be timezone-aware")
        remaining = (deadline_at.astimezone(UTC) - current.astimezone(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError(AskErrorCode.DEADLINE_EXCEEDED.value)
        return remaining

    def _agent_deadline(self, deadline_at: datetime, stage: AskAgentStage) -> datetime:
        if stage is AskAgentStage.REVIEWER:
            self._remaining(deadline_at)
            return deadline_at
        cutoff = deadline_at - timedelta(seconds=_REVIEW_RESERVE_SECONDS)
        self._remaining(cutoff)
        return cutoff

    def _repair_time_remains(self, deadline_at: datetime) -> bool:
        try:
            self._remaining(deadline_at - timedelta(seconds=_REVIEW_RESERVE_SECONDS))
        except TimeoutError:
            return False
        return True

    def _pipeline(self, run_id: str) -> PipelineDefinition:
        execution = self.storage.manager.get_execution(run_id)
        if execution is None or not execution.definition_json:
            raise RuntimeError(f"Ask run has no executable pipeline snapshot: {run_id}")
        snapshot = execution.definition_json
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        if not isinstance(snapshot, dict):
            raise RuntimeError("Ask pipeline definition snapshot is invalid")
        return parse_ask_pipeline(snapshot)

    def _fault(self, boundary_id: str) -> None:
        if self.fault_injector is not None:
            try:
                self.fault_injector(boundary_id)
            except AskFaultInjected:
                raise
            except Exception as error:
                raise AskFaultInjected(boundary_id) from error


__all__ = ["AskFaultInjected", "AskService"]
