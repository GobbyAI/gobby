"""Lifecycle event helpers for PipelineExecutor."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("gobby.workflows.pipeline_executor")


class PipelineExecutorEventsMixin:
    """Event, completion, and child-session cleanup helpers."""

    completion_registry: Any
    event_callback: Any
    session_manager: Any

    async def _emit_event(self, event: str, execution_id: str, **kwargs: Any) -> None:
        """Emit a pipeline event via the callback if configured.

        Args:
            event: Event type (pipeline_started, step_completed, etc.)
            execution_id: Pipeline execution ID
            **kwargs: Additional event data
        """
        if self.event_callback:
            try:
                await self.event_callback(event, execution_id, **kwargs)
            except (ValueError, RuntimeError, OSError):
                logger.warning(
                    "Failed to emit pipeline event",
                    extra={"event": event, "execution_id": execution_id},
                    exc_info=True,
                )

    async def _notify_completion(
        self,
        execution_id: str,
        status: str,
        pipeline_name: str,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Notify the completion registry that a pipeline finished.

        Fail-open: errors are logged but never propagate to the caller.
        """
        if not self.completion_registry:
            return
        try:
            result: dict[str, Any] = {
                "status": status,
                "pipeline_name": pipeline_name,
            }
            if outputs is not None:
                result["outputs"] = outputs
            if error is not None:
                result["error"] = error

            # Build targeted message for orchestration completion
            message = ""
            if outputs and str(outputs.get("orchestration_complete", "")).lower() in (
                "true",
                "1",
            ):
                session_task = outputs.get("session_task", "unknown")
                iteration = outputs.get("iteration", "?")
                message = (
                    f"Orchestration complete for task {session_task}. "
                    f"All tasks finished after {iteration} iterations."
                )

            await self.completion_registry.notify(execution_id, result, message=message)
        except Exception:
            logger.warning(
                f"Failed to notify completion registry for {execution_id}",
                exc_info=True,
            )

    def _close_pipeline_session(
        self,
        pipeline_session_id: str | None,
        caller_session_id: str | None,
    ) -> None:
        """Close the pipeline's child session after execution finishes.

        Pipeline sessions are implementation details - they should not
        linger in the user's session list. Fail-open: errors are logged
        but never propagate.
        """
        if (
            not pipeline_session_id
            or not self.session_manager
            or pipeline_session_id == caller_session_id
        ):
            return
        try:
            self.session_manager.update_status(pipeline_session_id, "deleted")
        except Exception:
            logger.warning(
                f"Failed to close pipeline session {pipeline_session_id}",
                exc_info=True,
            )
