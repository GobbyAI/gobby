"""Ask-specific resume and cancellation decisions."""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from gobby.ask.stages import (
    AskAttemptCheckpoint,
    AskAttemptStatus,
    AskErrorCode,
    AskOrchestrationState,
    AskStageStore,
)
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus


class AskAgentRecoveryRuntime(Protocol):
    def status(self, agent_run_id: str) -> str | None: ...

    def cancel(self, agent_run_id: str) -> Awaitable[None]: ...


class AskPermissionRevoker(Protocol):
    def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int: ...


@dataclass(frozen=True, slots=True)
class AskRecoveryDecision:
    state: AskOrchestrationState
    execution_status: ExecutionStatus
    current_step_id: str | None = None
    reattach_agent_run_id: str | None = None
    restart_attempt: AskAttemptCheckpoint | None = None


class AskRecoveryController:
    """Guard resumes against terminal state, expiry, and duplicate attempts."""

    def __init__(
        self,
        *,
        manager: LocalPipelineExecutionManager,
        stages: AskStageStore,
        permissions: AskPermissionRevoker,
        agents: AskAgentRecoveryRuntime,
    ) -> None:
        self.manager = manager
        self.stages = stages
        self.permissions = permissions
        self.agents = agents

    def inspect(
        self,
        run_id: str,
        *,
        project_id: str,
        now: datetime | None = None,
    ) -> AskRecoveryDecision:
        execution = self.manager.get_execution(run_id)
        if execution is None or execution.project_id != project_id:
            raise ValueError(f"Ask run not found: {run_id}")
        state = self.stages.get(run_id)
        if state is None:
            raise ValueError(f"Ask run has no durable orchestration state: {run_id}")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("Ask recovery clock must be timezone-aware")
        if current.astimezone(UTC) >= state.deadline_at:
            raise TimeoutError(AskErrorCode.DEADLINE_EXCEEDED.value)
        if execution.status in {ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED}:
            raise ValueError(f"Ask run cannot resume from {execution.status.value}")
        if execution.status is ExecutionStatus.RUNNING:
            raise ValueError("Ask run is already running")

        current_step_id = self._current_step_id(run_id)

        active = self._current_attempt(state)
        if active is None or active.submission_artifact is not None:
            return AskRecoveryDecision(
                state=state,
                execution_status=execution.status,
                current_step_id=current_step_id,
            )
        if active.agent_run_id is None:
            return AskRecoveryDecision(
                state=state,
                execution_status=execution.status,
                current_step_id=current_step_id,
                restart_attempt=active,
            )
        agent_status = self.agents.status(active.agent_run_id)
        if agent_status in {"pending", "running"}:
            return AskRecoveryDecision(
                state=state,
                execution_status=execution.status,
                current_step_id=current_step_id,
                reattach_agent_run_id=active.agent_run_id,
            )
        if agent_status == "interrupted":
            return AskRecoveryDecision(
                state=state,
                execution_status=execution.status,
                current_step_id=current_step_id,
                restart_attempt=active,
            )
        raise ValueError(f"Ask attempt cannot resume from agent status {agent_status!r}")

    def claim_resume(
        self,
        run_id: str,
        *,
        project_id: str,
        caller_session_id: str,
        decision: AskRecoveryDecision,
    ) -> AskOrchestrationState:
        """Atomically reserve one failed/interrupted Ask execution for its executor."""
        expected = decision.execution_status
        if expected not in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
            raise ValueError("Ask run is already being resumed")
        with self.manager.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT status, inputs_json FROM pipeline_executions
                WHERE id = %s AND project_id = %s AND pipeline_name = 'native-ask'
                FOR UPDATE
                """,
                (run_id, project_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Ask run not found: {run_id}")
            if str(row["status"]) != expected.value:
                raise ValueError("Ask run is already being resumed")
            if decision.current_step_id is not None:
                step_update = connection.execute(
                    """
                    UPDATE step_executions
                    SET status = %s, error = NULL, completed_at = NULL
                    WHERE execution_id = %s AND step_id = %s
                      AND status IN (%s, %s)
                    """,
                    (
                        StepStatus.PENDING.value,
                        run_id,
                        decision.current_step_id,
                        StepStatus.RUNNING.value,
                        StepStatus.FAILED.value,
                    ),
                )
                if step_update.rowcount != 1:
                    raise ValueError("Ask run stage changed before resume")
            document = json.loads(row["inputs_json"] or "{}")
            ask = document.get("ask")
            if not isinstance(ask, dict):
                raise RuntimeError("Ask execution inputs are invalid")
            runtime = ask.setdefault("runtime", {})
            if not isinstance(runtime, dict):
                raise RuntimeError("Ask runtime metadata is invalid")
            runtime["resume_claim"] = {
                "caller_session_id": caller_session_id,
                "expected_status": expected.value,
                "current_step_id": decision.current_step_id,
                "claimed_at": datetime.now(UTC).isoformat(),
            }
            updated = connection.execute(
                """
                UPDATE pipeline_executions
                SET status = %s, inputs_json = %s,
                    completed_at = NULL, updated_at = NOW()
                WHERE id = %s AND status = %s
                RETURNING id
                """,
                (
                    ExecutionStatus.PENDING.value,
                    json.dumps(document, sort_keys=True, separators=(",", ":")),
                    run_id,
                    expected.value,
                ),
            ).fetchone()
            if updated is None:
                raise ValueError("Ask run is already being resumed")
        state = self.stages.get(run_id)
        if state is None:
            raise RuntimeError(f"Ask run has no durable orchestration state: {run_id}")
        return state

    async def cancel(
        self,
        run_id: str,
        *,
        project_id: str,
        caller_session_id: str,
    ) -> AskOrchestrationState:
        execution = self.manager.get_execution(run_id)
        if execution is None or execution.project_id != project_id:
            raise ValueError(f"Ask run not found: {run_id}")
        state = self.stages.get(run_id)
        if state is None:
            raise ValueError(f"Ask run has no durable orchestration state: {run_id}")
        if execution.status is ExecutionStatus.CANCELLED:
            return state
        if execution.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}:
            raise ValueError(f"Ask run cannot cancel from {execution.status.value}")

        # Submission authority must disappear before any process cleanup begins.
        self.permissions.revoke_for_run(run_id, reason="cancelled")
        for attempt in state.attempts:
            if attempt.agent_run_id is None or attempt.status is not AskAttemptStatus.RUNNING:
                continue
            await self.agents.cancel(attempt.agent_run_id)
        return self.stages.terminate(
            run_id,
            status=ExecutionStatus.CANCELLED,
            stage=state.current_stage,
            boundary_id=f"cancelled:{caller_session_id}",
        )

    @staticmethod
    def _current_attempt(state: AskOrchestrationState) -> AskAttemptCheckpoint | None:
        for attempt in reversed(state.attempts):
            if attempt.status in {AskAttemptStatus.RESERVED, AskAttemptStatus.RUNNING}:
                return attempt
        return None

    def _current_step_id(self, run_id: str) -> str | None:
        steps = self.manager.get_steps_for_execution(run_id)
        for step in reversed(steps):
            if step.status in {StepStatus.RUNNING, StepStatus.FAILED}:
                return step.step_id
        return None


__all__ = ["AskAgentRecoveryRuntime", "AskRecoveryController", "AskRecoveryDecision"]
