"""Durable stage and attempt checkpoints for the native Ask pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from gobby.ask.contracts import (
    AskBinding,
    AskRunRecord,
    AskRunResult,
    EvidenceReference,
    ProfileSnapshot,
)
from gobby.ask.permissions import AskAgentStage
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus


class AskStage(StrEnum):
    BIND_PREPARE = "bind_prepare"
    SEED_QUERIES = "seed_queries"
    INVESTIGATOR = "investigator"
    VALIDATION = "validation"
    REVIEW = "review"
    REPAIR = "repair"
    PUBLISH = "publish"


class AskErrorCode(StrEnum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    AGENT_FAILED = "agent_failed"
    VALIDATION_FAILED = "validation_failed"


class AskAttemptStatus(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AskTypedError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: AskErrorCode
    message: str


class AskAttemptCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: AskAgentStage
    attempt: int = Field(ge=0)
    status: AskAttemptStatus = AskAttemptStatus.RESERVED
    agent_run_id: str | None = None
    successor_run_ids: tuple[str, ...] = ()
    submission_artifact: dict[str, Any] | None = None
    submission_hash: str | None = None
    evidence_manifest_hash: str | None = None
    completed_at: datetime | None = None


class AskBoundaryEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_id: str
    stage: AskStage
    recorded_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class AskOrchestrationState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    run_id: str
    status: str = ExecutionStatus.PENDING.value
    current_stage: AskStage = AskStage.BIND_PREPARE
    deadline_at: datetime
    answer_outcome: str | None = None
    typed_error: AskTypedError | None = None
    profiles: dict[str, str]
    tool_identities: tuple[str, ...]
    attempts: tuple[AskAttemptCheckpoint, ...] = ()
    repair_count: int = Field(default=0, ge=0, le=1)
    evidence_manifest_artifact: dict[str, Any] | None = None
    evidence_manifest_hash: str | None = None
    deterministic_validation_artifact: dict[str, Any] | None = None
    review_validation_artifact: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    boundaries: tuple[AskBoundaryEvent, ...] = ()


class AskStepCheckpoint(BaseModel):
    """Cumulative Ask checkpoint returned by one declared pipeline step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    run_id: str
    status: str
    current_stage: AskStage
    answer_outcome: str | None = None
    typed_error: AskTypedError | None = None
    attempts: tuple[AskAttemptCheckpoint, ...] = ()
    repair_count: int = Field(default=0, ge=0, le=1)
    repair: bool | None = None
    evidence_manifest_artifact: dict[str, Any] | None = None
    evidence_manifest_hash: str | None = None
    deterministic_validation_artifact: dict[str, Any] | None = None
    review_validation_artifact: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    boundaries: tuple[AskBoundaryEvent, ...] = ()


_STEP_ORDER = (
    "prepare",
    "seed",
    "investigate",
    "validate_initial",
    "review_initial",
    "admit_repair",
    "repair",
    "validate_repair",
    "review_repair",
    "publish",
)
_STEP_INDEX = {step_id: index for index, step_id in enumerate(_STEP_ORDER)}
_STEP_STAGES = {
    "prepare": AskStage.BIND_PREPARE,
    "seed": AskStage.SEED_QUERIES,
    "investigate": AskStage.INVESTIGATOR,
    "validate_initial": AskStage.VALIDATION,
    "review_initial": AskStage.REVIEW,
    "admit_repair": AskStage.REPAIR,
    "repair": AskStage.REPAIR,
    "validate_repair": AskStage.VALIDATION,
    "review_repair": AskStage.REVIEW,
    "publish": AskStage.PUBLISH,
}


_ASK_TOOL_IDENTITIES = (
    "gobby-ask:query_evidence",
    "gobby-ask:read_evidence",
    "gobby-ask:submit_answer",
    "gobby-ask:submit_review",
    "gobby-agents:end_agent_run",
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(raw: object, *, name: str) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    if not isinstance(raw, dict):
        raise RuntimeError(f"invalid persisted {name}")
    return dict(raw)


class AskStageStore:
    """Single writer for checkpoints owned by declared pipeline steps."""

    def __init__(
        self,
        manager: LocalPipelineExecutionManager,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.manager = manager
        self.now = now or (lambda: datetime.now(UTC))

    def initialize(self, record: AskRunRecord) -> AskOrchestrationState:
        profiles = {
            "investigator": record.investigator.identifier,
            "reviewer": record.reviewer.identifier,
        }

        def update(current: AskOrchestrationState | None) -> AskOrchestrationState:
            if current is not None:
                if current.deadline_at != record.binding.deadline_at:
                    raise RuntimeError("Ask orchestration deadline differs from immutable binding")
                return current
            return AskOrchestrationState(
                run_id=record.run_id,
                deadline_at=record.binding.deadline_at,
                profiles=profiles,
                tool_identities=_ASK_TOOL_IDENTITIES,
                boundaries=(self._event("bind:initialized", AskStage.BIND_PREPARE, {}),),
            )

        return self._mutate(record.run_id, update)

    def get(self, run_id: str) -> AskOrchestrationState | None:
        execution = self.manager.get_execution(run_id)
        if execution is None:
            return None
        steps = self.manager.get_steps_for_execution(run_id)
        rows: list[Mapping[str, Any]] = [
            {
                "step_id": item.step_id,
                "status": item.status.value,
            }
            for item in steps
        ]
        return self._project(
            run_id,
            status=execution.status.value,
            inputs_json=execution.inputs_json or "{}",
            step_rows=rows,
        )

    def step_output(self, run_id: str, step_id: str) -> dict[str, Any]:
        """Return the complete durable output for one declared Ask step."""
        if step_id not in _STEP_INDEX:
            raise ValueError(f"Unknown Ask pipeline step: {step_id}")
        state = self.get(run_id)
        if state is None:
            raise ValueError(f"Ask run has no orchestration state: {run_id}")
        checkpoint = AskStepCheckpoint(
            run_id=state.run_id,
            status=state.status,
            current_stage=state.current_stage,
            answer_outcome=state.answer_outcome,
            typed_error=state.typed_error,
            attempts=state.attempts,
            repair_count=state.repair_count,
            repair=(state.repair_count == 1) if step_id == "admit_repair" else None,
            evidence_manifest_artifact=state.evidence_manifest_artifact,
            evidence_manifest_hash=state.evidence_manifest_hash,
            deterministic_validation_artifact=state.deterministic_validation_artifact,
            review_validation_artifact=state.review_validation_artifact,
            publication=state.publication,
            boundaries=state.boundaries,
        )
        return checkpoint.model_dump(mode="json")

    def reserve_attempt(
        self,
        run_id: str,
        *,
        stage: AskAgentStage | AskStage,
        attempt: int,
        boundary_id: str,
    ) -> AskAttemptCheckpoint:
        agent_stage = self._agent_stage(stage)
        selected: AskAttemptCheckpoint | None = None

        def update(current: AskOrchestrationState | None) -> AskOrchestrationState:
            nonlocal selected
            state = self._required(current, run_id)
            for item in state.attempts:
                if item.stage is agent_stage and item.attempt == attempt:
                    selected = item
                    return state
            selected = AskAttemptCheckpoint(stage=agent_stage, attempt=attempt)
            return state.model_copy(
                update={
                    "current_stage": self._pipeline_stage(agent_stage),
                    "attempts": (*state.attempts, selected),
                    "boundaries": self._append_event(
                        state,
                        boundary_id,
                        self._pipeline_stage(agent_stage),
                        {"attempt": attempt},
                    ),
                }
            )

        self._mutate(run_id, update)
        if selected is None:
            raise RuntimeError("Ask attempt reservation was not persisted")
        return selected

    def bind_agent(
        self,
        run_id: str,
        *,
        stage: AskAgentStage | AskStage,
        attempt: int,
        agent_run_id: str,
        boundary_id: str,
        successor: bool = False,
    ) -> AskAttemptCheckpoint:
        agent_stage = self._agent_stage(stage)
        selected: AskAttemptCheckpoint | None = None

        def update(current: AskOrchestrationState | None) -> AskOrchestrationState:
            nonlocal selected
            state = self._required(current, run_id)
            items: list[AskAttemptCheckpoint] = []
            found = False
            for item in state.attempts:
                if item.stage is not agent_stage or item.attempt != attempt:
                    items.append(item)
                    continue
                found = True
                if item.agent_run_id not in {None, agent_run_id} and not successor:
                    raise RuntimeError("Ask attempt already has another native agent")
                successors = item.successor_run_ids
                if successor and agent_run_id not in successors:
                    successors = (*successors, agent_run_id)
                selected = item.model_copy(
                    update={
                        "agent_run_id": agent_run_id,
                        "successor_run_ids": successors,
                        "status": AskAttemptStatus.RUNNING,
                    }
                )
                items.append(selected)
            if not found:
                raise RuntimeError("Ask attempt must be reserved before agent binding")
            return state.model_copy(
                update={
                    "attempts": tuple(items),
                    "boundaries": self._append_event(
                        state,
                        boundary_id,
                        self._pipeline_stage(agent_stage),
                        {"attempt": attempt, "agent_run_id": agent_run_id},
                    ),
                }
            )

        self._mutate(run_id, update)
        if selected is None:
            raise RuntimeError("Ask native agent binding was not persisted")
        return selected

    def record_submission(
        self,
        run_id: str,
        *,
        stage: AskAgentStage,
        attempt: int,
        agent_run_id: str,
        artifact: dict[str, Any],
        submission_hash: str,
        evidence_manifest_hash: str,
        boundary_id: str,
    ) -> AskAttemptCheckpoint:
        selected: AskAttemptCheckpoint | None = None

        def update(current: AskOrchestrationState | None) -> AskOrchestrationState:
            nonlocal selected
            state = self._required(current, run_id)
            items: list[AskAttemptCheckpoint] = []
            for item in state.attempts:
                if item.stage is not stage or item.attempt != attempt:
                    items.append(item)
                    continue
                if item.agent_run_id != agent_run_id:
                    raise RuntimeError("Ask submission came from a superseded agent")
                if item.submission_artifact is not None:
                    if (
                        item.status is not AskAttemptStatus.COMPLETED
                        or item.submission_artifact != artifact
                        or item.submission_hash != submission_hash
                        or item.evidence_manifest_hash != evidence_manifest_hash
                    ):
                        raise RuntimeError("Ask attempt submission is immutable")
                    selected = item
                    items.append(item)
                    continue
                candidate = item.model_copy(
                    update={
                        "status": AskAttemptStatus.COMPLETED,
                        "submission_artifact": artifact,
                        "submission_hash": submission_hash,
                        "evidence_manifest_hash": evidence_manifest_hash,
                        "completed_at": self._now(),
                    }
                )
                selected = candidate
                items.append(selected)
            if selected is None:
                raise RuntimeError("Ask submission has no current attempt")
            return state.model_copy(
                update={
                    "attempts": tuple(items),
                    "boundaries": self._append_event(
                        state,
                        boundary_id,
                        self._pipeline_stage(stage),
                        {"attempt": attempt, "submission_hash": submission_hash},
                    ),
                }
            )

        self._mutate(run_id, update)
        if selected is None:
            raise RuntimeError("Ask submission checkpoint was not persisted")
        return selected

    def checkpoint(
        self,
        run_id: str,
        *,
        stage: AskStage,
        boundary_id: str,
        details: dict[str, Any] | None = None,
        **updates: Any,
    ) -> AskOrchestrationState:
        def update(current: AskOrchestrationState | None) -> AskOrchestrationState:
            state = self._required(current, run_id)
            return state.model_copy(
                update={
                    "current_stage": stage,
                    "boundaries": self._append_event(state, boundary_id, stage, details or {}),
                    **updates,
                }
            )

        return self._mutate(run_id, update)

    def terminate(
        self,
        run_id: str,
        *,
        status: ExecutionStatus,
        stage: AskStage,
        boundary_id: str,
        error: AskTypedError | None = None,
        answer_outcome: str | None = None,
        publication: dict[str, Any] | None = None,
    ) -> AskOrchestrationState:
        if status not in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise ValueError("Ask terminal checkpoint requires a terminal pipeline status")

        def update(current: AskOrchestrationState | None) -> AskOrchestrationState:
            state = self._required(current, run_id)
            return state.model_copy(
                update={
                    "status": status.value,
                    "current_stage": stage,
                    "typed_error": error,
                    "answer_outcome": answer_outcome,
                    "publication": publication,
                    "boundaries": self._append_event(state, boundary_id, stage, {}),
                }
            )

        return self._mutate(run_id, update, pipeline_status=status)

    def to_result(
        self,
        record: AskRunRecord,
        *,
        evidence: tuple[EvidenceReference, ...] = (),
    ) -> AskRunResult:
        state = self.get(record.run_id)
        if state is None:
            raise ValueError(f"Ask run has no orchestration state: {record.run_id}")
        result_artifact = state.publication.get("manifest") if state.publication else None
        return AskRunResult(
            run_id=record.run_id,
            status=state.status,
            current_stage=state.current_stage.value,
            answer_outcome=state.answer_outcome,
            typed_error=(
                {"code": state.typed_error.code.value, "message": state.typed_error.message}
                if state.typed_error
                else None
            ),
            deadline_at=state.deadline_at,
            profile_identities=state.profiles,
            tool_identities=state.tool_identities,
            artifact_manifest=result_artifact,
            attempt_count=len(state.attempts),
            repair_count=state.repair_count,
            binding=record.binding,
            evidence=evidence,
            result_artifact=result_artifact,
        )

    def _mutate(
        self,
        run_id: str,
        update: Callable[[AskOrchestrationState | None], AskOrchestrationState],
        *,
        pipeline_status: ExecutionStatus | None = None,
    ) -> AskOrchestrationState:
        with self.manager.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT project_id, status, inputs_json FROM pipeline_executions
                WHERE id = %s AND pipeline_name = 'native-ask'
                FOR NO KEY UPDATE
                """,
                (run_id,),
            ).fetchone()
            if row is None or (
                self.manager.project_id is not None
                and str(row["project_id"]) != self.manager.project_id
            ):
                raise ValueError(f"Ask run not found: {run_id}")
            step_rows: list[Mapping[str, Any]] = list(
                connection.execute(
                    """
                    SELECT step_id, status
                    FROM step_executions
                    WHERE execution_id = %s
                    ORDER BY id
                    FOR UPDATE
                    """,
                    (run_id,),
                ).fetchall()
            )
            current = self._project(
                run_id,
                status=str(row["status"]),
                inputs_json=str(row["inputs_json"] or "{}"),
                step_rows=step_rows,
            )
            state = update(current)
            document = _json_object(row["inputs_json"], name="pipeline inputs")
            ask = _json_object(document.get("ask"), name="Ask pipeline inputs")
            runtime = _json_object(ask.get("runtime", {}), name="Ask runtime metadata")
            runtime["orchestration"] = state.model_dump(mode="json")
            ask["runtime"] = runtime
            document["ask"] = ask
            if pipeline_status is not None:
                connection.execute(
                    """
                    UPDATE pipeline_executions SET inputs_json = %s, status = %s,
                        completed_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (_canonical_json(document), pipeline_status.value, run_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (_canonical_json(document), run_id),
                )
        return state

    def _project(
        self,
        run_id: str,
        *,
        status: str,
        inputs_json: str,
        step_rows: list[Mapping[str, Any]],
    ) -> AskOrchestrationState | None:
        inputs = _json_object(inputs_json, name="pipeline inputs")
        ask = inputs.get("ask")
        if not isinstance(ask, dict):
            raise RuntimeError("Ask pipeline inputs are missing")
        runtime = _json_object(ask.get("runtime", {}), name="Ask runtime metadata")
        body = runtime.get("orchestration")
        if body is None:
            return None
        state = AskOrchestrationState.model_validate(body)
        if state.run_id != run_id:
            raise RuntimeError("Ask orchestration checkpoint belongs to another run")
        binding = AskBinding.model_validate(ask.get("binding"))
        investigator = ProfileSnapshot.model_validate(ask.get("investigator"))
        reviewer = ProfileSnapshot.model_validate(ask.get("reviewer"))
        profiles = {
            "investigator": investigator.identifier,
            "reviewer": reviewer.identifier,
        }
        if state.deadline_at != binding.deadline_at or state.profiles != profiles:
            raise RuntimeError("Ask orchestration differs from immutable start binding")
        return state.model_copy(
            update={
                "status": status,
                "current_stage": self._derive_stage(step_rows, state),
                "typed_error": (
                    state.typed_error if status == ExecutionStatus.FAILED.value else None
                ),
            }
        )

    @staticmethod
    def _derive_stage(
        step_rows: list[Mapping[str, Any]],
        state: AskOrchestrationState,
    ) -> AskStage:
        running = [
            str(item["step_id"])
            for item in step_rows
            if str(item["step_id"]) in _STEP_INDEX
            and str(item["status"]) == StepStatus.RUNNING.value
        ]
        if running:
            return _STEP_STAGES[max(running, key=_STEP_INDEX.__getitem__)]
        return state.current_stage

    @staticmethod
    def _required(state: AskOrchestrationState | None, run_id: str) -> AskOrchestrationState:
        if state is None:
            raise ValueError(f"Ask run has no orchestration state: {run_id}")
        return state

    def _append_event(
        self,
        state: AskOrchestrationState,
        boundary_id: str,
        stage: AskStage,
        details: dict[str, Any],
    ) -> tuple[AskBoundaryEvent, ...]:
        if any(item.boundary_id == boundary_id for item in state.boundaries):
            return state.boundaries
        return (*state.boundaries, self._event(boundary_id, stage, details))

    def _event(
        self, boundary_id: str, stage: AskStage, details: dict[str, Any]
    ) -> AskBoundaryEvent:
        return AskBoundaryEvent(
            boundary_id=boundary_id,
            stage=stage,
            recorded_at=self._now(),
            details=details,
        )

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            raise ValueError("Ask stage clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _agent_stage(stage: AskAgentStage | AskStage) -> AskAgentStage:
        if isinstance(stage, AskAgentStage):
            return stage
        return {
            AskStage.INVESTIGATOR: AskAgentStage.INVESTIGATOR,
            AskStage.REVIEW: AskAgentStage.REVIEWER,
            AskStage.REPAIR: AskAgentStage.REPAIR,
        }[stage]

    @staticmethod
    def _pipeline_stage(stage: AskAgentStage) -> AskStage:
        return {
            AskAgentStage.INVESTIGATOR: AskStage.INVESTIGATOR,
            AskAgentStage.REVIEWER: AskStage.REVIEW,
            AskAgentStage.REPAIR: AskStage.REPAIR,
        }[stage]


__all__ = [
    "AskAttemptCheckpoint",
    "AskAttemptStatus",
    "AskBoundaryEvent",
    "AskErrorCode",
    "AskOrchestrationState",
    "AskStage",
    "AskStageStore",
    "AskTypedError",
]
