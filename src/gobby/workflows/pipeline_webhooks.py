"""Pipeline webhook notifier for sending HTTP notifications.

This module provides the WebhookNotifier class for sending webhook
notifications during pipeline execution events (approval pending,
completion, failure).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from gobby.workflows.webhook_executor import WebhookExecutor

if TYPE_CHECKING:
    from gobby.workflows.definitions import PipelineDefinition
    from gobby.workflows.pipeline_state import PipelineExecution

logger = logging.getLogger(__name__)


class WebhookNotifier:
    """Sends webhook notifications for pipeline execution events.

    Handles approval pending, completion, and failure notifications using
    the workflow webhook transport policy.
    """

    def __init__(self, base_url: str, executor: WebhookExecutor | None = None) -> None:
        """Initialize the webhook notifier.

        Args:
            base_url: Base URL for generating approve/reject URLs.
            executor: Optional hardened webhook transport.
        """
        self.base_url = base_url.rstrip("/")
        self.executor = executor or WebhookExecutor()

    async def notify_approval_pending(
        self,
        execution: PipelineExecution,
        pipeline: PipelineDefinition,
        step_id: str,
        token: str,
        message: str,
    ) -> None:
        """Send notification when approval is required.

        Args:
            execution: The pipeline execution state
            pipeline: The pipeline definition (contains webhook config)
            step_id: The step ID requiring approval
            token: The approval token for approve/reject URLs
            message: The approval message to display
        """
        if not pipeline.webhooks or not pipeline.webhooks.on_approval_pending:
            logger.debug("No on_approval_pending webhook configured for %s", pipeline.name)
            return

        endpoint = pipeline.webhooks.on_approval_pending
        payload = {
            "execution_id": execution.id,
            "pipeline_name": execution.pipeline_name,
            "step_id": step_id,
            "token": token,
            "message": message,
            "approve_url": f"{self.base_url}/api/pipelines/approve/{token}",
            "reject_url": f"{self.base_url}/api/pipelines/reject/{token}",
            "status": execution.status.value,
        }

        await self._send_webhook(endpoint.url, endpoint.method, endpoint.headers, payload)

    async def notify_complete(
        self,
        execution: PipelineExecution,
        pipeline: PipelineDefinition,
    ) -> None:
        """Send notification when pipeline completes successfully.

        Args:
            execution: The pipeline execution state
            pipeline: The pipeline definition (contains webhook config)
        """
        if not pipeline.webhooks or not pipeline.webhooks.on_complete:
            logger.debug("No on_complete webhook configured for %s", pipeline.name)
            return

        endpoint = pipeline.webhooks.on_complete

        # Parse outputs JSON if present
        outputs = None
        if execution.outputs_json:
            try:
                outputs = json.loads(execution.outputs_json)
            except json.JSONDecodeError:
                outputs = execution.outputs_json

        payload = {
            "execution_id": execution.id,
            "pipeline_name": execution.pipeline_name,
            "status": execution.status.value,
            "outputs": outputs,
            "completed_at": execution.completed_at,
        }

        await self._send_webhook(endpoint.url, endpoint.method, endpoint.headers, payload)

    async def notify_failure(
        self,
        execution: PipelineExecution,
        pipeline: PipelineDefinition,
        error: str,
    ) -> None:
        """Send notification when pipeline fails.

        Args:
            execution: The pipeline execution state
            pipeline: The pipeline definition (contains webhook config)
            error: The error message describing the failure
        """
        if not pipeline.webhooks or not pipeline.webhooks.on_failure:
            logger.debug("No on_failure webhook configured for %s", pipeline.name)
            return

        endpoint = pipeline.webhooks.on_failure
        payload = {
            "execution_id": execution.id,
            "pipeline_name": execution.pipeline_name,
            "status": execution.status.value,
            "error": error,
        }

        await self._send_webhook(endpoint.url, endpoint.method, endpoint.headers, payload)

    async def _send_webhook(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        """Send HTTP webhook request.

        Args:
            url: Target URL
            method: Supported HTTP method.
            headers: Request headers. Only explicit webhook secrets are interpolated.
            payload: JSON payload to send
        """
        try:
            result = await self.executor.execute(
                url=url,
                method=method,
                headers=headers,
                payload=payload,
                timeout=30,
            )
            if result.success:
                logger.debug("Webhook sent successfully to %s", url)
            else:
                logger.error(
                    "Webhook request failed: %s - %s",
                    result.status_code,
                    result.body or result.error,
                )
        except Exception as exc:
            logger.error("Failed to send webhook to %s: %s", url, exc, exc_info=True)
