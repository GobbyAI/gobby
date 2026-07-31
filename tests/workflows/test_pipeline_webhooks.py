"""Tests for hardened live pipeline webhook notifications."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.utils.webhook_transport import WebhookTransport, WebhookTransportResult
from gobby.workflows.definitions import PipelineDefinition, WebhookConfig, WebhookEndpoint
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution
from gobby.workflows.pipeline_webhooks import WebhookNotifier

pytestmark = pytest.mark.unit


@pytest.fixture
def execution() -> PipelineExecution:
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
def pipeline() -> PipelineDefinition:
    return PipelineDefinition(
        name="test-pipeline",
        webhooks=WebhookConfig(
            on_approval_pending=WebhookEndpoint(
                url="https://example.com/approval",
                headers={"Authorization": "Bearer ${API_TOKEN}"},
            ),
            on_complete=WebhookEndpoint(url="https://example.com/complete"),
            on_failure=WebhookEndpoint(url="https://example.com/failure"),
        ),
        steps=[{"id": "step1", "exec": "echo test"}],
    )


@pytest.fixture
def transport() -> MagicMock:
    transport = MagicMock(spec=WebhookTransport)
    transport.execute = AsyncMock(
        return_value=WebhookTransportResult(success=True, status_code=200)
    )
    return transport


@pytest.fixture
def notifier(transport: MagicMock) -> WebhookNotifier:
    return WebhookNotifier(base_url="https://gobby.local", transport=transport)


async def test_approval_payload_does_not_expand_process_environment(
    execution: PipelineExecution,
    pipeline: PipelineDefinition,
    notifier: WebhookNotifier,
    transport: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_TOKEN", "process-secret")

    await notifier.notify_approval_pending(
        execution=execution,
        pipeline=pipeline,
        step_id="deploy",
        token="approval-token-123",
        message="Approve deployment?",
    )

    call = transport.execute.await_args.kwargs
    assert call["url"] == "https://example.com/approval"
    assert call["method"] == "POST"
    assert call["headers"] == {"Authorization": "Bearer ${API_TOKEN}"}
    assert call["payload"] == {
        "execution_id": "pe-abc123def456",
        "pipeline_name": "test-pipeline",
        "step_id": "deploy",
        "token": "approval-token-123",
        "message": "Approve deployment?",
        "approve_url": "https://gobby.local/api/pipelines/approve/approval-token-123",
        "reject_url": "https://gobby.local/api/pipelines/reject/approval-token-123",
        "status": "running",
    }


async def test_completion_payload_contains_outputs(
    execution: PipelineExecution,
    pipeline: PipelineDefinition,
    notifier: WebhookNotifier,
    transport: MagicMock,
) -> None:
    execution.status = ExecutionStatus.COMPLETED
    execution.outputs_json = '{"result": "success"}'

    await notifier.notify_complete(execution=execution, pipeline=pipeline)

    payload = transport.execute.await_args.kwargs["payload"]
    assert payload["status"] == "completed"
    assert payload["outputs"] == {"result": "success"}


async def test_failure_payload_contains_error(
    execution: PipelineExecution,
    pipeline: PipelineDefinition,
    notifier: WebhookNotifier,
    transport: MagicMock,
) -> None:
    execution.status = ExecutionStatus.FAILED

    await notifier.notify_failure(execution=execution, pipeline=pipeline, error="step failed")

    payload = transport.execute.await_args.kwargs["payload"]
    assert payload["status"] == "failed"
    assert payload["error"] == "step failed"


@pytest.mark.parametrize(
    ("notification", "field"),
    [
        ("approval", "on_approval_pending"),
        ("complete", "on_complete"),
        ("failure", "on_failure"),
    ],
)
async def test_missing_endpoint_is_ignored(
    notification: str,
    field: str,
    execution: PipelineExecution,
    pipeline: PipelineDefinition,
    notifier: WebhookNotifier,
    transport: MagicMock,
) -> None:
    assert pipeline.webhooks is not None
    setattr(pipeline.webhooks, field, None)

    if notification == "approval":
        await notifier.notify_approval_pending(execution, pipeline, "step", "token", "message")
    elif notification == "complete":
        await notifier.notify_complete(execution, pipeline)
    else:
        await notifier.notify_failure(execution, pipeline, "error")

    transport.execute.assert_not_awaited()


@pytest.mark.parametrize("method", ["DELETE", "GET", "PATCH", "POST", "PUT"])
async def test_supported_methods_are_forwarded_explicitly(
    method: str,
    execution: PipelineExecution,
    pipeline: PipelineDefinition,
    notifier: WebhookNotifier,
    transport: MagicMock,
) -> None:
    assert pipeline.webhooks is not None
    assert pipeline.webhooks.on_complete is not None
    pipeline.webhooks.on_complete.method = method

    await notifier.notify_complete(execution, pipeline)

    assert transport.execute.await_args.kwargs["method"] == method


async def test_transport_failure_is_logged_without_raising(
    execution: PipelineExecution,
    pipeline: PipelineDefinition,
    notifier: WebhookNotifier,
    transport: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport.execute.side_effect = ValueError("unsafe webhook target")

    with caplog.at_level(logging.ERROR):
        await notifier.notify_complete(execution, pipeline)

    assert "unsafe webhook target" in caplog.text


def test_default_notifier_uses_restricted_shared_transport() -> None:
    notifier = WebhookNotifier(base_url="https://gobby.local")

    assert isinstance(notifier.transport, WebhookTransport)
    assert notifier.transport.allow_private_addresses is False
