"""Pipeline webhook notifier for sending HTTP notifications.

This module provides the WebhookNotifier class for sending webhook
notifications during pipeline execution events (approval pending,
completion, failure).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

from gobby.utils.env import expand_env_mapping

if TYPE_CHECKING:
    from gobby.workflows.definitions import PipelineDefinition
    from gobby.workflows.pipeline_state import PipelineExecution

logger = logging.getLogger(__name__)


class WebhookNotifier:
    """Sends webhook notifications for pipeline execution events.

    Handles approval pending, completion, and failure notifications
    with environment variable expansion in headers.
    """

    def __init__(self, base_url: str = "http://localhost:7778"):
        """Initialize the webhook notifier.

        Args:
            base_url: Base URL for generating approve/reject URLs.
                     Defaults to localhost Gobby daemon URL.
        """
        self.base_url = base_url.rstrip("/")

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
            logger.debug(f"No on_approval_pending webhook configured for {pipeline.name}")
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
            logger.debug(f"No on_complete webhook configured for {pipeline.name}")
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
            logger.debug(f"No on_failure webhook configured for {pipeline.name}")
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
            method: HTTP method (POST, PUT, etc.)
            headers: Request headers (supports ${VAR} expansion)
            payload: JSON payload to send
        """
        expanded_headers = expand_env_mapping(headers) or {}

        try:
            async with httpx.AsyncClient() as client:
                if method.upper() == "POST":
                    response = await client.post(
                        url=url,
                        headers=expanded_headers,
                        json=payload,
                        timeout=30.0,
                    )
                elif method.upper() == "PUT":
                    response = await client.put(
                        url=url,
                        headers=expanded_headers,
                        json=payload,
                        timeout=30.0,
                    )
                else:
                    logger.warning(f"Unsupported webhook method: {method}")
                    return

                if response.status_code >= 400:
                    logger.error(
                        f"Webhook request failed: {response.status_code} - {response.text}"
                    )
                else:
                    logger.debug(f"Webhook sent successfully to {url}")

        except Exception as e:
            logger.error(f"Failed to send webhook to {url}: {e}")
