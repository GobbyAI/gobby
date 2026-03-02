"""Approval gate management for pipeline workflows."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING, Any

from gobby.workflows.pipeline_state import (
    ApprovalRequired,
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

if TYPE_CHECKING:
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.definitions import PipelineDefinition, PipelineStep

logger = logging.getLogger(__name__)


class ApprovalManager:
    """Manages approval gates for pipeline steps."""

    def __init__(
        self,
        execution_manager: LocalPipelineExecutionManager,
        webhook_notifier: Any | None = None,
        event_callback: Any | None = None,
    ) -> None:
        self.execution_manager = execution_manager
        self.webhook_notifier = webhook_notifier
        self.event_callback = event_callback

    async def _emit_event(self, event: str, execution_id: str, **kwargs: Any) -> None:
        """Emit a pipeline event via the callback if configured."""
        if self.event_callback:
            try:
                await self.event_callback(event, execution_id, **kwargs)
            except (ValueError, RuntimeError, OSError):
                logger.warning(
                    "Failed to emit pipeline event",
                    extra={"event": event, "execution_id": execution_id},
                    exc_info=True,
                )

    async def check_approval_gate(
        self,
        step: PipelineStep,
        execution: PipelineExecution,
        step_execution: StepExecution,
        pipeline: PipelineDefinition,
    ) -> None:
        """Check if a step has an approval gate and handle it.

        If the step requires approval, this method:
        1. Generates a unique approval token
        2. Updates the step and execution status to WAITING_APPROVAL
        3. Calls the webhook notifier if configured
        4. Raises ApprovalRequired to pause execution

        Args:
            step: The step to check for approval requirement
            execution: The current pipeline execution record
            step_execution: The current step execution record
            pipeline: The pipeline definition (for webhook config)

        Raises:
            ApprovalRequired: If the step requires approval
        """
        # Check if step has approval gate
        if not step.approval or not step.approval.required:
            return

        # Generate unique approval token
        token = secrets.token_urlsafe(24)

        # Get approval message
        message = step.approval.message or f"Approval required for step '{step.id}'"

        # Update step status to WAITING_APPROVAL and store token
        timeout_seconds = step.approval.timeout_seconds if step.approval else None
        await asyncio.to_thread(
            self.execution_manager.update_step_execution,
            step_execution_id=step_execution.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_token=token,
            approval_timeout_seconds=timeout_seconds,
        )

        # Update execution status to WAITING_APPROVAL
        await asyncio.to_thread(
            self.execution_manager.update_execution_status,
            execution_id=execution.id,
            status=ExecutionStatus.WAITING_APPROVAL,
            resume_token=token,
        )

        # Call webhook notifier if configured
        if self.webhook_notifier:
            try:
                await self.webhook_notifier.notify_approval_pending(
                    execution=execution,
                    step_id=step.id,
                    token=token,
                    message=message,
                    pipeline=pipeline,
                )
            except (OSError, RuntimeError):
                logger.warning(
                    "Failed to send approval webhook",
                    extra={"execution_id": execution.id, "step_id": step.id},
                    exc_info=True,
                )

        # Emit approval_required event
        await self._emit_event(
            "approval_required",
            execution.id,
            step_id=step.id,
            step_name=getattr(step, "name", step.id),
            message=message,
            token=token,
        )

        # Raise to pause execution
        raise ApprovalRequired(
            execution_id=execution.id,
            step_id=step.id,
            token=token,
            message=message,
        )

    async def approve_step(
        self,
        token: str,
        approved_by: str | None = None,
    ) -> PipelineExecution:
        """Approve a pipeline step.

        Args:
            token: The approval token from the ApprovalRequired exception
            approved_by: Optional identifier of the approver

        Returns:
            The updated PipelineExecution record

        Raises:
            ValueError: If the token is invalid or not found
        """
        # Find the step by approval token
        step = self.execution_manager.get_step_by_approval_token(token)
        if not step:
            raise ValueError(f"Invalid approval token: {token}")

        # Mark step as approved
        self.execution_manager.update_step_execution(
            step_execution_id=step.id,
            status=StepStatus.COMPLETED,
            approved_by=approved_by,
        )

        # Get the execution
        execution = self.execution_manager.get_execution(step.execution_id)
        if not execution:
            raise ValueError(f"Execution {step.execution_id} not found")

        return execution

    async def reject_step(
        self,
        token: str,
        rejected_by: str | None = None,
    ) -> PipelineExecution:
        """Reject a pipeline step.

        Args:
            token: The approval token from the ApprovalRequired exception
            rejected_by: Optional identifier of the rejector

        Returns:
            The updated PipelineExecution record

        Raises:
            ValueError: If the token is invalid or not found
        """
        # Find the step by approval token
        step = self.execution_manager.get_step_by_approval_token(token)
        if not step:
            raise ValueError(f"Invalid approval token: {token}")

        # Mark step as failed
        error_msg = "Rejected"
        if rejected_by:
            error_msg += f" by {rejected_by}"

        self.execution_manager.update_step_execution(
            step_execution_id=step.id,
            status=StepStatus.FAILED,
            error=error_msg,
        )

        # Set execution status to CANCELLED
        execution = self.execution_manager.update_execution_status(
            execution_id=step.execution_id,
            status=ExecutionStatus.CANCELLED,
        )

        if not execution:
            raise ValueError(f"Execution {step.execution_id} not found")

        return execution
