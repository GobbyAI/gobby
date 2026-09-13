"""Execution of declared native Ask pipeline stages."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from gobby.ask.agents import AskAgentRuntime, AskAgentSpec
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.claims import AnswerDraft, ReviewerResult, canonical_hash
from gobby.ask.contracts import AskRunRecord
from gobby.ask.evidence import EvidenceAdmission
from gobby.ask.evidence_runtime import (
    AskEvidenceRuntime,
    AskEvidenceSession,
    AskSnapshotManager,
    EvidenceFactory,
    EvidenceManifestFactory,
    PreparedAskSnapshot,
    build_evidence_manifest,
    pinned_blobs,
)
from gobby.ask.permissions import AskAgentStage, AskPermissionRuntime
from gobby.ask.publication import PublicationError, PublishedAnswer, publish_answer
from gobby.ask.stages import AskAttemptCheckpoint, AskOrchestrationState, AskStage, AskStageStore
from gobby.ask.storage import AskRunStorage
from gobby.ask.validation import validate_claims, validate_review
from gobby.ask.validation_models import (
    ClaimValidationReport,
    EvidenceManifest,
    ReviewValidationReport,
)
from gobby.workflows.pipeline_state import ExecutionStatus

FaultInjector = Callable[[str], None]
RemainingSeconds = Callable[[datetime], float]
AgentDeadline = Callable[[datetime, AskAgentStage], datetime]
RepairTimeRemains = Callable[[datetime], bool]


@dataclass(slots=True)
class _RunResources:
    snapshot: PreparedAskSnapshot
    artifacts: AskArtifactStore


class AskStageRuntime:
    """Own ephemeral resources while declared pipeline steps own scheduling."""

    def __init__(
        self,
        *,
        storage: AskRunStorage,
        stages: AskStageStore,
        snapshot_manager: AskSnapshotManager,
        agents: AskAgentRuntime,
        permissions: AskPermissionRuntime,
        state_root: Path | None,
        evidence_factory: EvidenceFactory | None,
        evidence_manifest_factory: EvidenceManifestFactory | None,
        remaining_seconds: RemainingSeconds,
        agent_deadline: AgentDeadline,
        repair_time_remains: RepairTimeRemains,
        fault_injector: FaultInjector,
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
                self.storage, record, snapshot, artifacts
            )
        )
        self.remaining_seconds = remaining_seconds
        self.agent_deadline = agent_deadline
        self.repair_time_remains = repair_time_remains
        self.fault = fault_injector
        self.resources: dict[str, _RunResources] = {}
        self._publication_tasks: dict[str, asyncio.Task[PublishedAnswer]] = {}
        self._publication_cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self.evidence = AskEvidenceRuntime(
            stages=stages,
            manifest_factory=manifest_factory,
            remaining_seconds=remaining_seconds,
            fault_injector=fault_injector,
        )

    async def prepare(
        self,
        record: AskRunRecord,
        *,
        project_root: Path,
    ) -> dict[str, Any]:
        inputs = await asyncio.to_thread(self.storage.execution_inputs, record.run_id)
        if Path(str(inputs["project_root"])).resolve() != project_root.resolve():
            raise ValueError("Ask prepare root differs from immutable execution context")
        state = self._state(record.run_id)
        if not self._has_boundary(state, "bind:running"):
            await asyncio.to_thread(
                self.stages.checkpoint,
                record.run_id,
                stage=AskStage.BIND_PREPARE,
                boundary_id="bind:running",
                details={"repository_root": str(project_root.resolve())},
            )
            self.fault("bind:running")
        resources = await self.ensure_resources(record, project_root.resolve())
        state = self._state(record.run_id)
        if not self._has_boundary(state, "bind:snapshot-prepared"):
            await asyncio.to_thread(
                self.stages.checkpoint,
                record.run_id,
                stage=AskStage.BIND_PREPARE,
                boundary_id="bind:snapshot-prepared",
                details={"source_root": str(resources.snapshot.source_root)},
            )
            self.fault("bind:snapshot-prepared")
        return await asyncio.to_thread(self.stages.step_output, record.run_id, "prepare")

    async def seed(self, record: AskRunRecord) -> dict[str, Any]:
        await self.ensure_resources(record)
        state = self._state(record.run_id)
        if not self._has_boundary(state, "seed:complete"):
            await self._seed(
                self.evidence.admission(record.run_id),
                record.request.question,
                record.binding.deadline_at,
            )
            await asyncio.to_thread(
                self.stages.checkpoint,
                record.run_id,
                stage=AskStage.SEED_QUERIES,
                boundary_id="seed:complete",
            )
            self.fault("seed:complete")
            await self.evidence.persist_manifest(
                record.run_id,
                stage=AskStage.SEED_QUERIES,
            )
        elif self._state(record.run_id).evidence_manifest_artifact is None:
            await self.evidence.persist_manifest(
                record.run_id,
                stage=AskStage.SEED_QUERIES,
            )
        return await asyncio.to_thread(self.stages.step_output, record.run_id, "seed")

    async def spawn(
        self,
        record: AskRunRecord,
        *,
        stage: AskAgentStage,
        attempt: int,
        caller_session_id: str,
    ) -> dict[str, Any]:
        self._validate_stage_attempt(stage, attempt)
        resources = await self.ensure_resources(record)
        evidence = await self.evidence.load_current(record.run_id)
        if stage in {AskAgentStage.INVESTIGATOR, AskAgentStage.REPAIR}:
            await self._answer_attempt(
                record,
                resources.snapshot,
                resources.artifacts,
                evidence,
                stage=stage,
                attempt=attempt,
                caller_session_id=caller_session_id,
            )
        else:
            draft, accepted_evidence = await self.answer_product(record, attempt)
            await self._review_attempt(
                record,
                resources.snapshot,
                resources.artifacts,
                draft,
                accepted_evidence,
                await self.claim_validation(record),
                attempt=attempt,
                caller_session_id=caller_session_id,
            )
        step_id = {
            (AskAgentStage.INVESTIGATOR, 0): "investigate",
            (AskAgentStage.REVIEWER, 0): "review_initial",
            (AskAgentStage.REPAIR, 1): "repair",
            (AskAgentStage.REVIEWER, 1): "review_repair",
        }[(stage, attempt)]
        return await asyncio.to_thread(self.stages.step_output, record.run_id, step_id)

    async def validate(
        self,
        record: AskRunRecord,
        *,
        stage: AskAgentStage,
        attempt: int,
    ) -> dict[str, Any]:
        if (stage, attempt) not in {
            (AskAgentStage.INVESTIGATOR, 0),
            (AskAgentStage.REPAIR, 1),
        }:
            raise ValueError("Ask validation stage or attempt is invalid")
        resources = await self.ensure_resources(record)
        draft, evidence = await self.answer_product(record, attempt)
        await self._validate(record, resources.snapshot, resources.artifacts, draft, evidence)
        step_id = "validate_repair" if attempt else "validate_initial"
        return await asyncio.to_thread(self.stages.step_output, record.run_id, step_id)

    async def admit_repair(self, record: AskRunRecord) -> dict[str, Any]:
        await self.ensure_resources(record)
        state = self._state(record.run_id)
        draft, _evidence = await self.answer_product(record, 0)
        repair = self._needs_repair(
            draft,
            await self.claim_validation(record),
            await self.review_validation(record),
        ) and self.repair_time_remains(record.binding.deadline_at)
        boundary_id = "repair:admitted" if repair else "repair:skipped"
        if not self._has_boundary(state, boundary_id):
            await asyncio.to_thread(
                self.stages.checkpoint,
                record.run_id,
                stage=AskStage.REPAIR,
                boundary_id=boundary_id,
                repair_count=1 if repair else 0,
            )
            self.fault(boundary_id)
        return await asyncio.to_thread(
            self.stages.step_output,
            record.run_id,
            "admit_repair",
        )

    async def publish(self, record: AskRunRecord) -> dict[str, Any]:
        resources = await self.ensure_resources(record)
        state = self._state(record.run_id)
        if state.publication is not None:
            return await asyncio.to_thread(
                self.stages.step_output,
                record.run_id,
                "publish",
            )
        attempt = 1 if state.repair_count else 0
        draft, evidence = await self.answer_product(record, attempt)
        deterministic = await self.claim_validation(record)
        review = await self.review_validation(record)
        if review.diagnostics:
            codes = ", ".join(item.code for item in review.diagnostics)
            raise PublicationError(f"mandatory Ask review failed identity validation: {codes}")

        def check_publication_authority() -> None:
            self.remaining_seconds(record.binding.deadline_at)
            execution = self.storage.manager.get_execution(record.run_id)
            if execution is None or execution.project_id != record.binding.project_id:
                raise PublicationError("Ask publication execution identity changed")
            if execution.status not in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
                raise PublicationError(
                    f"Ask publication is not authorized from {execution.status.value}"
                )

        publish = partial(
            publish_answer,
            resources.artifacts,
            draft,
            evidence,
            deterministic,
            review,
            request=record.request.model_dump(mode="json"),
            binding=evidence.repository_binding.model_dump(mode="json"),
            profiles={
                "investigator": record.investigator.model_dump(mode="json"),
                "reviewer": record.reviewer.model_dump(mode="json"),
            },
            tool_identities=state.tool_identities,
            attempt_history=[item.model_dump(mode="json") for item in state.attempts],
            deadline_check=check_publication_authority,
        )
        publication = await self._run_publication(record.run_id, publish)
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
        boundary_id = f"publish:{publication.manifest_sha256}"
        await asyncio.to_thread(
            self.stages.checkpoint,
            record.run_id,
            stage=AskStage.PUBLISH,
            boundary_id=boundary_id,
            status=ExecutionStatus.COMPLETED.value,
            answer_outcome=publication.outcome,
            publication=publication_body,
        )
        self.fault(boundary_id)
        return await asyncio.to_thread(self.stages.step_output, record.run_id, "publish")

    async def ensure_resources(
        self,
        record: AskRunRecord,
        project_root: Path | None = None,
    ) -> _RunResources:
        existing = self.resources.get(record.run_id)
        if existing is not None:
            return existing
        artifacts = AskArtifactStore(self.state_root, record.binding.project_id, record.run_id)
        current = await asyncio.to_thread(self.storage.get, record.run_id)
        if current is None:
            raise RuntimeError("Ask run disappeared during resource preparation")
        generation = current.generation
        if generation is None:
            if project_root is None:
                inputs = await asyncio.to_thread(self.storage.execution_inputs, record.run_id)
                project_root = Path(str(inputs["project_root"])).resolve()
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
        admission = self.evidence_factory(record, snapshot, artifacts)
        self.evidence.activate(record, snapshot, artifacts, admission)
        resources = _RunResources(snapshot=snapshot, artifacts=artifacts)
        self.resources[record.run_id] = resources
        return resources

    async def release(self, run_id: str) -> None:
        publication = self._publication_tasks.get(run_id)
        if publication is not None and not publication.done():
            if run_id not in self._publication_cleanup_tasks:
                cleanup = asyncio.create_task(
                    self._release_after_publication(run_id, publication),
                    name=f"native-ask-publication-cleanup:{run_id}",
                )
                self._publication_cleanup_tasks[run_id] = cleanup
            return
        self._publication_tasks.pop(run_id, None)
        await self._release_resources(run_id)

    async def wait_for_publication_cleanup(self, run_id: str) -> None:
        """Wait for any tracked synchronous publisher and deferred resource release."""
        publication = self._publication_tasks.get(run_id)
        if publication is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(publication)
            if self._publication_tasks.get(run_id) is publication:
                self._publication_tasks.pop(run_id, None)
        cleanup = self._publication_cleanup_tasks.get(run_id)
        if cleanup is not None and cleanup is not asyncio.current_task():
            with contextlib.suppress(BaseException):
                await asyncio.shield(cleanup)

    async def _run_publication(
        self,
        run_id: str,
        operation: Callable[[], PublishedAnswer],
    ) -> PublishedAnswer:
        task = self._publication_tasks.get(run_id)
        if task is None or task.done():
            task = asyncio.create_task(
                asyncio.to_thread(operation),
                name=f"native-ask-publication:{run_id}",
            )
            self._publication_tasks[run_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._publication_tasks.get(run_id) is task:
                self._publication_tasks.pop(run_id, None)

    async def _release_after_publication(
        self,
        run_id: str,
        publication: asyncio.Task[PublishedAnswer],
    ) -> None:
        try:
            with contextlib.suppress(BaseException):
                await asyncio.shield(publication)
            if self._publication_tasks.get(run_id) is publication:
                self._publication_tasks.pop(run_id, None)
            await self._release_resources(run_id)
        finally:
            if self._publication_cleanup_tasks.get(run_id) is asyncio.current_task():
                self._publication_cleanup_tasks.pop(run_id, None)

    async def _release_resources(self, run_id: str) -> None:
        resources = self.resources.pop(run_id, None)
        self.evidence.deactivate(run_id)
        if resources is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.snapshot_manager.release,
                    resources.snapshot,
                    artifacts=resources.artifacts,
                )

    async def answer_product(
        self,
        record: AskRunRecord,
        attempt: int,
    ) -> tuple[AnswerDraft, EvidenceManifest]:
        stage = AskAgentStage.REPAIR if attempt else AskAgentStage.INVESTIGATOR
        checkpoint = self._attempt(record.run_id, stage, attempt)
        if checkpoint.submission_artifact is None or checkpoint.submission_hash is None:
            raise RuntimeError("Ask investigator has no accepted answer submission")
        resources = await self.ensure_resources(record)
        body = await asyncio.to_thread(
            resources.artifacts.read_body,
            checkpoint.submission_artifact,
        )
        draft = AnswerDraft.model_validate(body)
        if draft.content_hash != checkpoint.submission_hash:
            raise RuntimeError("Ask answer submission hash changed after checkpoint")
        if checkpoint.evidence_manifest_hash is None:
            raise RuntimeError("Ask answer submission omitted its evidence manifest hash")
        evidence = await self.evidence.load_current(
            record.run_id,
            expected_hash=checkpoint.evidence_manifest_hash,
        )
        return draft, evidence

    async def claim_validation(self, record: AskRunRecord) -> ClaimValidationReport:
        pointer = self._state(record.run_id).deterministic_validation_artifact
        if pointer is None:
            raise RuntimeError("Ask deterministic validation is unavailable")
        resources = await self.ensure_resources(record)
        return ClaimValidationReport.model_validate(
            await asyncio.to_thread(resources.artifacts.read_body, pointer)
        )

    async def review_validation(self, record: AskRunRecord) -> ReviewValidationReport:
        pointer = self._state(record.run_id).review_validation_artifact
        if pointer is None:
            raise RuntimeError("Ask review validation is unavailable")
        resources = await self.ensure_resources(record)
        return ReviewValidationReport.model_validate(
            await asyncio.to_thread(resources.artifacts.read_body, pointer)
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
            timeout_seconds=self.remaining_seconds(record.binding.deadline_at),
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
        self.fault(boundary_id)
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
        self.fault(f"{stage.value}:{attempt}:reserved")
        if checkpoint.submission_artifact is not None:
            return checkpoint
        repair_feedback = None
        if stage is AskAgentStage.REPAIR:
            initial = self._attempt(record.run_id, AskAgentStage.INVESTIGATOR, 0)
            if initial.submission_artifact is None:
                raise RuntimeError("Ask repair requires the initial submitted draft")
            prior = AnswerDraft.model_validate(
                await asyncio.to_thread(artifacts.read_body, initial.submission_artifact)
            )
            if prior.content_hash != initial.submission_hash:
                raise RuntimeError("Ask initial answer hash changed before repair")
            deterministic = await self.claim_validation(record)
            review = await self.review_validation(record)
            if (
                deterministic.draft_hash != prior.content_hash
                or review.draft_hash != prior.content_hash
            ):
                raise RuntimeError("Ask repair feedback does not bind the initial draft")
            draft = prior.model_dump(mode="json")
            repair_feedback = {
                "claim_validation": deterministic.model_dump(mode="json"),
                "review_validation": review.model_dump(mode="json"),
            }
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
            deadline_at=self.agent_deadline(record.binding.deadline_at, stage),
            draft=draft,
            repair_feedback=repair_feedback,
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
            self.fault(f"{stage.value}:{attempt}:launched")
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
                raise RuntimeError(f"Ask attempt cannot resume from agent status {status!r}")

        status = await self.agents.wait(
            agent_run_id,
            timeout=self.remaining_seconds(spec.deadline_at),
        )
        checkpoint = self._attempt(record.run_id, stage, attempt)
        if status != "success" or checkpoint.submission_artifact is None:
            raise RuntimeError(f"Ask agent ended as {status} without a valid submission")
        self.fault(f"{stage.value}:{attempt}:submitted")
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
            timeout_seconds=self.remaining_seconds(record.binding.deadline_at),
        )
        digest = canonical_hash(report.model_dump(mode="json"))
        boundary_id = f"validation:{draft.content_hash}:{digest}"
        await asyncio.to_thread(
            self.stages.checkpoint,
            record.run_id,
            stage=AskStage.VALIDATION,
            boundary_id=boundary_id,
            deterministic_validation_artifact=pointer,
        )
        self.fault(boundary_id)
        return report

    async def _seed(
        self,
        admission: AskEvidenceSession,
        question: str,
        deadline_at: datetime,
    ) -> None:
        cutoff = deadline_at - timedelta(seconds=60)
        self.remaining_seconds(cutoff)
        await admission.query(
            "search",
            {"lane": "content", "query": question, "paths": [], "limit": 1000},
        )
        self.remaining_seconds(cutoff)
        await admission.query(
            "search",
            {"lane": "lexical_symbol", "query": question, "paths": [], "limit": 1000},
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

    def _state(self, run_id: str) -> AskOrchestrationState:
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
        for checkpoint in self._state(run_id).attempts:
            if checkpoint.stage is stage and checkpoint.attempt == attempt:
                return checkpoint
        raise RuntimeError("Ask attempt checkpoint disappeared")

    @staticmethod
    def _validate_stage_attempt(stage: AskAgentStage, attempt: int) -> None:
        if (stage, attempt) not in {
            (AskAgentStage.INVESTIGATOR, 0),
            (AskAgentStage.REVIEWER, 0),
            (AskAgentStage.REPAIR, 1),
            (AskAgentStage.REVIEWER, 1),
        }:
            raise ValueError("Ask agent stage or attempt is invalid")

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
    def _has_boundary(state: Any, boundary_id: str) -> bool:
        return any(event.boundary_id == boundary_id for event in state.boundaries)


__all__ = ["AskStageRuntime"]
