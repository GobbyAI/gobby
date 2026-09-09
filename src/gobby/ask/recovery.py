"""Ask-specific resume and cancellation decisions."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from gobby.ask.permissions import AskPermissionStore
from gobby.ask.stages import (
    AskAttemptCheckpoint,
    AskAttemptStatus,
    AskErrorCode,
    AskOrchestrationState,
    AskStageStore,
)
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_state import ExecutionStatus


class AskAgentRecoveryRuntime(Protocol):
    def status(self, agent_run_id: str) -> str | None: ...

    def cancel(self, agent_run_id: str) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class AskRecoveryDecision:
    state: AskOrchestrationState
    reattach_agent_run_id: str | None = None
    restart_attempt: AskAttemptCheckpoint | None = None


class AskRecoveryController:
    """Guard resumes against terminal state, expiry, and duplicate attempts."""

    def __init__(
        self,
        *,
        manager: LocalPipelineExecutionManager,
        stages: AskStageStore,
        permissions: AskPermissionStore,
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
        if execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.FAILED,
        }:
            raise ValueError(f"Ask run cannot resume from {execution.status.value}")

        active = self._current_attempt(state)
        if active is None or active.submission_artifact is not None:
            return AskRecoveryDecision(state=state)
        if active.agent_run_id is None:
            return AskRecoveryDecision(state=state, restart_attempt=active)
        agent_status = self.agents.status(active.agent_run_id)
        if agent_status in {"pending", "running"}:
            return AskRecoveryDecision(
                state=state,
                reattach_agent_run_id=active.agent_run_id,
            )
        return AskRecoveryDecision(state=state, restart_attempt=active)

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


__all__ = ["AskAgentRecoveryRuntime", "AskRecoveryController", "AskRecoveryDecision"]
