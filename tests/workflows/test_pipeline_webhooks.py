"""Tests for WebhookNotifier class for pipeline notifications.

TDD tests for pipeline webhook notifications including approval pending,
completion, and failure notifications.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.workflows.definitions import PipelineDefinition, WebhookConfig, WebhookEndpoint
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution
from gobby.workflows.pipeline_webhooks import WebhookNotifier

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_execution() -> PipelineExecution:
    """Create a mock pipeline execution."""
    return PipelineExecution(
        id="pe-abc123def456",
        pipeline_name="test-pipeline",
        project_id="proj-123",
        status=ExecutionStatus.RUNNING,
        created_at="2026-02-01T12:00:00Z",
        updated_at="2026-02-01T12:00:00Z",
        session_id="sess-123",
    )


@pytest.fixture
def webhook_config() -> WebhookConfig:
    """Create a webhook config with all endpoints."""
    return WebhookConfig(
        on_approval_pending=WebhookEndpoint(
            url="https://example.com/approval",
            method="POST",
            headers={"Authorization": "Bearer ${API_TOKEN}"},
        ),
        on_complete=WebhookEndpoint(
            url="https://example.com/complete",
            method="POST",
            headers={"Content-Type": "application/json"},
        ),
        on_failure=WebhookEndpoint(
            url="https://example.com/failure",
            method="POST",
        ),
    )


@pytest.fixture
def pipeline_with_webhooks(webhook_config: WebhookConfig) -> PipelineDefinition:
    """Create a pipeline definition with webhooks configured."""
    return PipelineDefinition(
        name="test-pipeline",
        webhooks=webhook_config,
        steps=[{"id": "step1", "exec": "echo test"}],
    )


@pytest.fixture
def pipeline_without_webhooks() -> PipelineDefinition:
    """Create a pipeline definition without webhooks."""
    return PipelineDefinition(
        name="test-pipeline",
        steps=[{"id": "step1", "exec": "echo test"}],
    )


class TestNotifyApprovalPending:
    """Tests for notify_approval_pending() method."""

    @pytest.mark.asyncio
    async def test_sends_post_with_correct_payload(
        self, mock_execution: PipelineExecution, pipeline_with_webhooks: PipelineDefinition
    ) -> None:
        """Test that approval notification sends correct payload."""
        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await notifier.notify_approval_pending(
                execution=mock_execution,
                pipeline=pipeline_with_webhooks,
                step_id="deploy",
                token="approval-token-123",
                message="Approve deployment to production?",
            )

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args

            # Check URL
            assert call_args.kwargs["url"] == "https://example.com/approval"

            # Check payload fields
            payload = call_args.kwargs["json"]
            assert payload["execution_id"] == "pe-abc123def456"
            assert payload["pipeline_name"] == "test-pipeline"
            assert payload["step_id"] == "deploy"
            assert payload["token"] == "approval-token-123"
            assert payload["message"] == "Approve deployment to production?"
            assert "approve_url" in payload
            assert "reject_url" in payload
            assert (
                payload["approve_url"]
                == "https://gobby.local/api/pipelines/approve/approval-token-123"
            )
            assert (
                payload["reject_url"]
                == "https://gobby.local/api/pipelines/reject/approval-token-123"
            )

    @pytest.mark.asyncio
    async def test_expands_env_vars_in_headers(
        self, mock_execution: PipelineExecution, pipeline_with_webhooks: PipelineDefinition
    ) -> None:
        """Test that ${VAR} patterns in headers are expanded from env."""
        notifier = WebhookNotifier(base_url="https://gobby.local")

        with (
            patch("httpx.AsyncClient") as mock_client_class,
            patch.dict("os.environ", {"API_TOKEN": "secret-token-value"}),
        ):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await notifier.notify_approval_pending(
                execution=mock_execution,
                pipeline=pipeline_with_webhooks,
                step_id="deploy",
                token="token-123",
                message="Approve?",
            )

            call_args = mock_client.post.call_args
            headers = call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer secret-token-value"

    @pytest.mark.asyncio
    async def test_handles_missing_webhooks_gracefully(
        self, mock_execution: PipelineExecution, pipeline_without_webhooks: PipelineDefinition
    ) -> None:
        """Test that missing webhooks config doesn't raise error."""
        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            await notifier.notify_approval_pending(
                execution=mock_execution,
                pipeline=pipeline_without_webhooks,
                step_id="deploy",
                token="token-123",
                message="Approve?",
            )
            assert mock_client_class.call_count == 0


class TestNotifyComplete:
    """Tests for notify_complete() method."""

    @pytest.mark.asyncio
    async def test_sends_completion_payload(
        self, mock_execution: PipelineExecution, pipeline_with_webhooks: PipelineDefinition
    ) -> None:
        """Test that completion notification sends correct payload."""
        mock_execution.status = ExecutionStatus.COMPLETED
        mock_execution.outputs_json = '{"result": "success"}'

        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await notifier.notify_complete(
                execution=mock_execution,
                pipeline=pipeline_with_webhooks,
            )

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args

            # Check URL
            assert call_args.kwargs["url"] == "https://example.com/complete"

            # Check payload
            payload = call_args.kwargs["json"]
            assert payload["execution_id"] == "pe-abc123def456"
            assert payload["pipeline_name"] == "test-pipeline"
            assert payload["status"] == "completed"
            assert payload["outputs"] == {"result": "success"}

    @pytest.mark.asyncio
    async def test_handles_missing_on_complete_webhook(
        self, mock_execution: PipelineExecution
    ) -> None:
        """Test that missing on_complete webhook doesn't raise error."""
        pipeline = PipelineDefinition(
            name="test-pipeline",
            webhooks=WebhookConfig(on_failure=WebhookEndpoint(url="https://example.com/fail")),
            steps=[{"id": "step1", "exec": "echo test"}],
        )
        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            await notifier.notify_complete(execution=mock_execution, pipeline=pipeline)
            assert mock_client_class.call_count == 0


class TestNotifyFailure:
    """Tests for notify_failure() method."""

    @pytest.mark.asyncio
    async def test_sends_error_payload(
        self, mock_execution: PipelineExecution, pipeline_with_webhooks: PipelineDefinition
    ) -> None:
        """Test that failure notification sends error payload."""
        mock_execution.status = ExecutionStatus.FAILED

        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await notifier.notify_failure(
                execution=mock_execution,
                pipeline=pipeline_with_webhooks,
                error="Step 'deploy' failed with exit code 1",
            )

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args

            # Check URL
            assert call_args.kwargs["url"] == "https://example.com/failure"

            # Check payload
            payload = call_args.kwargs["json"]
            assert payload["execution_id"] == "pe-abc123def456"
            assert payload["pipeline_name"] == "test-pipeline"
            assert payload["status"] == "failed"
            assert payload["error"] == "Step 'deploy' failed with exit code 1"

    @pytest.mark.asyncio
    async def test_handles_missing_on_failure_webhook(
        self, mock_execution: PipelineExecution
    ) -> None:
        """Test that missing on_failure webhook doesn't raise error."""
        pipeline = PipelineDefinition(
            name="test-pipeline",
            webhooks=WebhookConfig(on_complete=WebhookEndpoint(url="https://example.com/done")),
            steps=[{"id": "step1", "exec": "echo test"}],
        )
        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            await notifier.notify_failure(
                execution=mock_execution,
                pipeline=pipeline,
                error="Some error",
            )
            assert mock_client_class.call_count == 0


class TestWebhookErrors:
    """Tests for webhook error handling."""

    @pytest.mark.asyncio
    async def test_logs_error_on_http_failure(
        self, mock_execution: PipelineExecution, pipeline_with_webhooks: PipelineDefinition
    ) -> None:
        """Test that HTTP errors are logged but don't raise."""
        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                return_value=MagicMock(status_code=500, text="Server Error")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await notifier.notify_complete(
                execution=mock_execution,
                pipeline=pipeline_with_webhooks,
            )
            assert mock_client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_handles_network_error_gracefully(
        self, mock_execution: PipelineExecution, pipeline_with_webhooks: PipelineDefinition
    ) -> None:
        """Test that network errors don't crash the notifier."""
        notifier = WebhookNotifier(base_url="https://gobby.local")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await notifier.notify_complete(
                execution=mock_execution,
                pipeline=pipeline_with_webhooks,
            )
            assert mock_client.post.await_count == 1
