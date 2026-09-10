"""Native Ask orchestration over immutable snapshots and managed agents."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gobby.ask.agents import AskAgentRuntime, AskAgentSpec
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.claims import AnswerDraft, ReviewerResult, canonical_hash
from gobby.ask.contracts import (
    AskRequest,
    AskRunRecord,
    AskRunResult,
)
from gobby.ask.evidence import EvidenceAdmission
from gobby.ask.evidence_runtime import (
    AskEvidenceRuntime,
    AskEvidenceSession,
    AskSnapshotManager,
    EvidenceFactory,
    EvidenceManifestFactory,
    PreparedAskSnapshot,
    build_evidence_manifest,
    evidence_references,
    pinned_blobs,
)
from gobby.ask.permissions import AskAgentStage, AskPermissionRuntime
from gobby.ask.publication import PublicationError, publish_answer
from gobby.ask.recovery import AskRecoveryController
from gobby.ask.snapshots import SnapshotDriftError
from gobby.ask.stages import (
    AskAttemptCheckpoint,
    AskErrorCode,
    AskOrchestrationState,
    AskStage,
    AskStageStore,
    AskTypedError,
)
from gobby.ask.storage import AskRunStorage
from gobby.ask.validation import (
    ClaimValidationReport,
    EvidenceManifest,
    ReviewValidationReport,
    validate_claims,
    validate_review,
)
from gobby.utils.session_context import get_current_agent_run_id
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
        state_root: Path | None,
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
        self.state_root = state_root
        self.evidence_factory = evidence_factory or self._default_evidence_factory
        manifest_factory = evidence_manifest_factory or (
            lambda record, snapshot, artifacts: build_evidence_manifest(
                self.storage,
                record,
                snapshot,
                artifacts,
            )
        )
        self.fault_injector = fault_injector
        self.now = now or (lambda: datetime.now(UTC))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.evidence = AskEvidenceRuntime(
            stages=stages,
            manifest_factory=manifest_factory,
            remaining_seconds=self._remaining,
            fault_injector=self._fault,
        )

    async def start(
        self,
        request: AskRequest,
        *,
        project_root: Path,
        caller_session_id: str,
    ) -> AskRunResult:
        record = await asyncio.to_thread(self.storage.start, request, project_root)
        await asyncio.to_thread(self.stages.initialize, record)
        self._ensure_task(record, project_root.resolve(), caller_session_id)
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
        record = self._record(run_id, project_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError:
                raise TimeoutError(f"timed out waiting for Ask run {run_id}") from None
        return await asyncio.to_thread(self._result, record)

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
        await asyncio.to_thread(recovery.inspect, run_id, project_id=project_id)
        await asyncio.to_thread(
            self.storage.manager.update_execution_status,
            run_id,
            ExecutionStatus.RUNNING,
        )
        repository_root = self._repository_root(run_id)
        self._ensure_task(record, repository_root, caller_session_id)
        return await asyncio.to_thread(self._result, record)

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
        project_root: Path,
        caller_session_id: str,
    ) -> None:
        existing = self._tasks.get(record.run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run(record, project_root, caller_session_id),
            name=f"native-ask:{record.run_id}",
        )
        self._tasks[record.run_id] = task
        task.add_done_callback(lambda completed: self._task_done(record.run_id, completed))

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("native Ask background task escaped its boundary", exc_info=error)

    async def _run(
        self,
        record: AskRunRecord,
        project_root: Path,
        caller_session_id: str,
    ) -> None:
        snapshot: PreparedAskSnapshot | None = None
        artifacts = self._artifacts(record)
        try:
            await asyncio.to_thread(
                self.storage.manager.update_execution_status,
                record.run_id,
                ExecutionStatus.RUNNING,
            )
            state = await asyncio.to_thread(
                self.stages.checkpoint,
                record.run_id,
                stage=AskStage.BIND_PREPARE,
                boundary_id="bind:running",
                status=ExecutionStatus.RUNNING.value,
                details={"repository_root": str(project_root)},
            )
            self._fault("bind:running")
            if self.storage.get_snapshot_generation(record.run_id) is None:
                snapshot = await self.snapshot_manager.prepare_async(
                    run_id=record.run_id,
                    repository_root=project_root,
                    artifacts=artifacts,
                )
            else:
                snapshot = await self.snapshot_manager.recover_async(
                    run_id=record.run_id,
                    artifacts=artifacts,
                )
            await asyncio.to_thread(
                self.stages.checkpoint,
                record.run_id,
                stage=AskStage.BIND_PREPARE,
                boundary_id="bind:snapshot-prepared",
                details={"source_root": str(snapshot.source_root)},
            )
            self._fault("bind:snapshot-prepared")

            admission = self.evidence_factory(record, snapshot, artifacts)
            self.evidence.activate(record, snapshot, artifacts, admission)
            state = self._required_state(record.run_id)
            if not self._has_boundary(state, "seed:complete"):
                await self._seed(admission, record.request.question, record.binding.deadline_at)
                await asyncio.to_thread(
                    self.stages.checkpoint,
                    record.run_id,
                    stage=AskStage.SEED_QUERIES,
                    boundary_id="seed:complete",
                )
                self._fault("seed:complete")
            evidence = await self.evidence.persist_manifest(
                record.run_id,
                stage=AskStage.SEED_QUERIES,
            )

            draft, evidence = await self._answer_attempt(
                record,
                snapshot,
                artifacts,
                evidence,
                stage=AskAgentStage.INVESTIGATOR,
                attempt=0,
                caller_session_id=caller_session_id,
            )
            deterministic = await self._validate(record, snapshot, artifacts, draft, evidence)
            review = await self._review_attempt(
                record,
                snapshot,
                artifacts,
                draft,
                evidence,
                deterministic,
                attempt=0,
                caller_session_id=caller_session_id,
            )

            if self._needs_repair(draft, deterministic, review) and self._repair_time_remains(
                record.binding.deadline_at
            ):
                await asyncio.to_thread(
                    self.stages.checkpoint,
                    record.run_id,
                    stage=AskStage.REPAIR,
                    boundary_id="repair:admitted",
                    repair_count=1,
                )
                self._fault("repair:admitted")
                draft, evidence = await self._answer_attempt(
                    record,
                    snapshot,
                    artifacts,
                    evidence,
                    stage=AskAgentStage.REPAIR,
                    attempt=1,
                    caller_session_id=caller_session_id,
                )
                deterministic = await self._validate(record, snapshot, artifacts, draft, evidence)
                review = await self._review_attempt(
                    record,
                    snapshot,
                    artifacts,
                    draft,
                    evidence,
                    deterministic,
                    attempt=1,
                    caller_session_id=caller_session_id,
                )

            if review.diagnostics:
                codes = ", ".join(item.code for item in review.diagnostics)
                raise PublicationError(f"mandatory Ask review failed identity validation: {codes}")
            publication = await asyncio.to_thread(
                publish_answer,
                artifacts,
                draft,
                evidence,
                deterministic,
                review,
                request=record.request.model_dump(mode="json"),
                binding=evidence.snapshot_binding.model_dump(mode="json"),
                profiles={
                    "investigator": record.investigator.model_dump(mode="json"),
                    "reviewer": record.reviewer.model_dump(mode="json"),
                },
                tool_identities=self._required_state(record.run_id).tool_identities,
                attempt_history=[
                    item.model_dump(mode="json")
                    for item in self._required_state(record.run_id).attempts
                ],
            )
            publication_body = {
                "root": str(publication.root),
                "manifest_sha256": publication.manifest_sha256,
                "outcome": publication.outcome,
                "claim_ids": list(publication.claim_ids),
                "manifest": {
                    "root": str(publication.root),
                    "sha256": publication.manifest_sha256,
                },
            }
            await asyncio.to_thread(
                self.stages.terminate,
                record.run_id,
                status=ExecutionStatus.COMPLETED,
                stage=AskStage.PUBLISH,
                boundary_id=f"publish:{publication.manifest_sha256}",
                answer_outcome=publication.outcome,
                publication=publication_body,
            )
            self._fault(f"publish:{publication.manifest_sha256}")
        except (asyncio.CancelledError, AskFaultInjected):
            raise
        except TimeoutError as error:
            await self._fail(record, AskErrorCode.DEADLINE_EXCEEDED, str(error))
        except SnapshotDriftError as error:
            await self._fail(record, AskErrorCode.SNAPSHOT_MISMATCH, str(error))
        except (PublicationError, ValueError) as error:
            await self._fail(record, AskErrorCode.VALIDATION_FAILED, str(error))
        except Exception as error:
            await self._fail(record, AskErrorCode.AGENT_FAILED, str(error))
        finally:
            self.evidence.deactivate(record.run_id)
            if snapshot is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.snapshot_manager.release,
                        snapshot,
                        artifacts=artifacts,
                    )

    async def _answer_attempt(
        self,
        record: AskRunRecord,
        snapshot: PreparedAskSnapshot,
        artifacts: AskArtifactStore,
        evidence: EvidenceManifest,
        *,
        stage: AskAgentStage,
        attempt: int,
        caller_session_id: str,
    ) -> tuple[AnswerDraft, EvidenceManifest]:
        checkpoint = await self._run_agent(
            record,
            snapshot,
            artifacts,
            evidence,
            stage=stage,
            attempt=attempt,
            caller_session_id=caller_session_id,
            draft=None,
        )
        if checkpoint.submission_artifact is None:
            raise RuntimeError("Ask investigator exited without a durable answer submission")
        body = await asyncio.to_thread(artifacts.read_body, checkpoint.submission_artifact)
        draft = AnswerDraft.model_validate(body)
        if draft.content_hash != checkpoint.submission_hash:
            raise RuntimeError("Ask answer submission hash changed after checkpoint")
        if checkpoint.evidence_manifest_hash is None:
            raise RuntimeError("Ask answer submission omitted its evidence manifest hash")
        accepted_evidence = await self.evidence.load_current(
            record.run_id,
            expected_hash=checkpoint.evidence_manifest_hash,
        )
        return draft, accepted_evidence

    async def _review_attempt(
        self,
        record: AskRunRecord,
        snapshot: PreparedAskSnapshot,
        artifacts: AskArtifactStore,
        draft: AnswerDraft,
        evidence: EvidenceManifest,
        deterministic: ClaimValidationReport,
        *,
        attempt: int,
        caller_session_id: str,
    ) -> ReviewValidationReport:
        checkpoint = await self._run_agent(
            record,
            snapshot,
            artifacts,
            evidence,
            stage=AskAgentStage.REVIEWER,
            attempt=attempt,
            caller_session_id=caller_session_id,
            draft=draft.model_dump(mode="json"),
        )
        if checkpoint.submission_artifact is None:
            raise RuntimeError("Ask reviewer exited without a durable review submission")
        body = await asyncio.to_thread(artifacts.read_body, checkpoint.submission_artifact)
        review = ReviewerResult.model_validate(body)
        report = validate_review(draft, evidence, deterministic, review)
        pointer = await asyncio.to_thread(
            artifacts.write_body,
            "review-validation",
            report.model_dump(mode="json"),
            timeout_seconds=self._remaining(record.binding.deadline_at),
        )
        boundary_id = (
            f"review-validation:{attempt}:{canonical_hash(report.model_dump(mode='json'))}"
        )
        await asyncio.to_thread(
            self.stages.checkpoint,
            record.run_id,
            stage=AskStage.REVIEW,
            boundary_id=boundary_id,
            review_validation_artifact=pointer,
        )
        self._fault(boundary_id)
        return report

    async def _run_agent(
        self,
        record: AskRunRecord,
        snapshot: PreparedAskSnapshot,
        artifacts: AskArtifactStore,
        evidence: EvidenceManifest,
        *,
        stage: AskAgentStage,
        attempt: int,
        caller_session_id: str,
        draft: dict[str, Any] | None,
    ) -> AskAttemptCheckpoint:
        checkpoint = await asyncio.to_thread(
            self.stages.reserve_attempt,
            record.run_id,
            stage=stage,
            attempt=attempt,
            boundary_id=f"{stage.value}:{attempt}:reserved",
        )
        self._fault(f"{stage.value}:{attempt}:reserved")
        if checkpoint.submission_artifact is not None:
            return checkpoint
        profile = record.reviewer if stage is AskAgentStage.REVIEWER else record.investigator
        scratch_root = artifacts.run_root / "scratch" / f"{stage.value}-{attempt}"
        spec = AskAgentSpec(
            run_id=record.run_id,
            project_id=record.binding.project_id,
            stage=stage,
            attempt=attempt,
            question=record.request.question,
            profile=profile,
            source_root=snapshot.source_root,
            scratch_root=scratch_root,
            caller_session_id=caller_session_id,
            evidence_manifest_hash=evidence.content_hash,
            deadline_at=self._agent_deadline(record.binding.deadline_at, stage),
            draft=draft,
        )
        agent_run_id = checkpoint.agent_run_id
        if agent_run_id is None:

            def bind(new_run_id: str, runtime_profile: Any) -> None:
                self.permissions.activate(
                    ask_run_id=record.run_id,
                    stage=stage,
                    attempt=attempt,
                    agent_run_id=new_run_id,
                    runtime_profile=runtime_profile,
                    scratch_root=scratch_root,
                )
                self.stages.bind_agent(
                    record.run_id,
                    stage=stage,
                    attempt=attempt,
                    agent_run_id=new_run_id,
                    boundary_id=f"{stage.value}:{attempt}:launched",
                )

            agent_run_id = await self.agents.launch(spec, bind)
            self._fault(f"{stage.value}:{attempt}:launched")
        else:
            status = self.agents.status(agent_run_id)
            if status == "interrupted":
                original_run_id = agent_run_id

                def replace(new_run_id: str, _runtime_profile: Any) -> None:
                    self.permissions.replace_for_resume(
                        original_agent_run_id=original_run_id,
                        successor_agent_run_id=new_run_id,
                    )
                    self.stages.bind_agent(
                        record.run_id,
                        stage=stage,
                        attempt=attempt,
                        agent_run_id=new_run_id,
                        boundary_id=f"{stage.value}:{attempt}:successor:{new_run_id}",
                        successor=True,
                    )

                agent_run_id = await self.agents.launch(spec, replace)
            elif status not in {"pending", "running"}:
                raise RuntimeError(f"Ask agent failed before submission: {status or 'missing'}")

        timeout = self._remaining(spec.deadline_at)
        status = await self.agents.wait(agent_run_id, timeout=timeout)
        checkpoint = self._attempt(record.run_id, stage, attempt)
        if status != "success" or checkpoint.submission_artifact is None:
            raise RuntimeError(f"Ask agent ended as {status} without a valid submission")
        self._fault(f"{stage.value}:{attempt}:submitted")
        return checkpoint

    async def _validate(
        self,
        record: AskRunRecord,
        snapshot: PreparedAskSnapshot,
        artifacts: AskArtifactStore,
        draft: AnswerDraft,
        evidence: EvidenceManifest,
    ) -> ClaimValidationReport:
        source_blobs = await asyncio.to_thread(pinned_blobs, snapshot, evidence)
        report = validate_claims(draft, evidence, pinned_blobs=source_blobs)
        pointer = await asyncio.to_thread(
            artifacts.write_body,
            "claim-validation",
            report.model_dump(mode="json"),
            timeout_seconds=self._remaining(record.binding.deadline_at),
        )
        digest = canonical_hash(report.model_dump(mode="json"))
        await asyncio.to_thread(
            self.stages.checkpoint,
            record.run_id,
            stage=AskStage.VALIDATION,
            boundary_id=f"validation:{draft.content_hash}:{digest}",
            deterministic_validation_artifact=pointer,
        )
        self._fault(f"validation:{draft.content_hash}:{digest}")
        return report

    async def _seed(
        self,
        admission: AskEvidenceSession,
        question: str,
        deadline_at: datetime,
    ) -> None:
        cutoff = deadline_at - timedelta(seconds=_REVIEW_RESERVE_SECONDS)
        self._remaining(cutoff)
        await admission.query(
            "search",
            {"lane": "content", "query": question, "paths": [], "limit": 1000},
        )
        self._remaining(cutoff)
        await admission.query(
            "search",
            {"lane": "lexical_symbol", "query": question, "paths": [], "limit": 1000},
        )

    async def _fail(
        self,
        record: AskRunRecord,
        code: AskErrorCode,
        message: str,
    ) -> None:
        state = self.stages.get(record.run_id)
        if state is None or state.status in {"completed", "cancelled", "failed"}:
            return
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
        )

    def _default_evidence_factory(
        self,
        record: AskRunRecord,
        snapshot: PreparedAskSnapshot,
        artifacts: AskArtifactStore,
    ) -> AskEvidenceSession:
        runtime = getattr(snapshot, "runtime", None)
        if runtime is None:
            raise RuntimeError("Ask snapshot omitted its managed index runtime")
        return EvidenceAdmission(
            run_id=record.run_id,
            runtime=runtime,
            permitted_operations={"search", "read", "graph"},
            page_size=1024 * 1024,
            artifacts=artifacts,
            storage=self.storage,
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
            raise ValueError(f"Ask run not found: {run_id}")
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

    def _attempt(
        self,
        run_id: str,
        stage: AskAgentStage,
        attempt: int,
    ) -> AskAttemptCheckpoint:
        for checkpoint in self._required_state(run_id).attempts:
            if checkpoint.stage is stage and checkpoint.attempt == attempt:
                return checkpoint
        raise RuntimeError("Ask attempt checkpoint disappeared")

    def _repository_root(self, run_id: str) -> Path:
        state = self._required_state(run_id)
        for event in state.boundaries:
            root = event.details.get("repository_root")
            if event.boundary_id == "bind:running" and isinstance(root, str):
                return Path(root)
        raise RuntimeError("Ask recovery has no persisted repository root")

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

    @staticmethod
    def _needs_repair(
        draft: AnswerDraft,
        deterministic: ClaimValidationReport,
        review: ReviewValidationReport,
    ) -> bool:
        claim_ids = {claim.id for claim in draft.claims}
        return (
            not deterministic.is_valid
            or set(review.accepted_claim_ids) != claim_ids
            or bool(review.missing_question_parts)
            or bool(review.diagnostics)
        )

    @staticmethod
    def _has_boundary(state: AskOrchestrationState, boundary_id: str) -> bool:
        return any(event.boundary_id == boundary_id for event in state.boundaries)

    def _fault(self, boundary_id: str) -> None:
        if self.fault_injector is not None:
            try:
                self.fault_injector(boundary_id)
            except AskFaultInjected:
                raise
            except Exception as error:
                raise AskFaultInjected(boundary_id) from error


__all__ = ["AskFaultInjected", "AskService"]
