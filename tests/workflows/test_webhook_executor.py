"""
Tests for WebhookExecutor.

TDD Red Phase: These tests should FAIL initially because WebhookExecutor doesn't exist yet.
"""

import asyncio
import socket
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from gobby.workflows.webhook_executor import MAX_RESPONSE_BYTES, WebhookExecutor, WebhookResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def public_dns_resolution() -> Iterator[None]:
    """Resolve test webhook hosts to a deterministic public address."""
    result = {
        "hostname": "api.example.com",
        "host": "93.184.216.34",
        "port": 443,
        "family": socket.AF_INET,
        "proto": 0,
        "flags": 0,
    }
    with patch(
        "gobby.workflows.webhook_executor.DefaultResolver.resolve",
        new=AsyncMock(return_value=[result]),
    ):
        yield


def create_mock_response(
    status: int = 200,
    body: str = "{}",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Create a mock aiohttp response with proper async context manager support."""
    mock_response = MagicMock()
    mock_response.status = status
    mock_response.content.readexactly = AsyncMock(
        return_value=body.encode()[: MAX_RESPONSE_BYTES + 1]
    )
    mock_response.get_encoding.return_value = "utf-8"
    mock_response.headers = headers or {}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    return mock_response


def create_mock_session(responses: MagicMock | list[MagicMock]) -> MagicMock:
    """Create a mock aiohttp session that returns the given responses.

    Args:
        responses: List of mock responses or a single response.
                  If a list, responses are returned in order on each request call.
    """
    if not isinstance(responses, list):
        responses = [responses]

    call_index = [0]

    def get_response(*args: object, **kwargs: object) -> MagicMock:
        idx = min(call_index[0], len(responses) - 1)
        call_index[0] += 1
        return responses[idx]

    mock_session = MagicMock()
    mock_session.request = MagicMock(side_effect=get_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mock_session


@pytest.fixture
def mock_template_engine() -> MagicMock:
    """Create a mock template engine for variable interpolation."""
    engine = MagicMock()
    engine.render.side_effect = lambda tmpl, ctx: tmpl  # Pass through by default
    return engine


@pytest.fixture
def mock_webhook_registry() -> dict[str, dict[str, Any]]:
    """Create a mock webhook registry for webhook_id resolution."""
    return {
        "slack_alerts": {
            "url": "https://hooks.slack.com/services/xxx",
            "headers": {"Content-Type": "application/json"},
        },
        "jira_api": {
            "url": "https://api.jira.com/webhook",
            "headers": {"Authorization": "Bearer default-token"},
        },
    }


@pytest.fixture
def mock_secrets() -> dict[str, str]:
    """Create a mock secrets provider."""
    return {
        "API_KEY": "secret-api-key-123",
        "SLACK_TOKEN": "xoxb-slack-token",
    }


@pytest.fixture
def executor(
    mock_template_engine: MagicMock,
    mock_webhook_registry: dict[str, dict[str, Any]],
    mock_secrets: dict[str, str],
) -> WebhookExecutor:
    """Create a WebhookExecutor instance with mocked dependencies."""
    return WebhookExecutor(
        template_engine=mock_template_engine,
        webhook_registry=mock_webhook_registry,
        secrets=mock_secrets,
    )


class TestWebhookExecutorSuccessPath:
    """Tests for successful webhook execution."""

    async def test_executor_makes_http_request_with_correct_method(
        self, executor: WebhookExecutor
    ) -> None:
        """Executor should make HTTP request with the configured method."""
        mock_response = create_mock_response(status=200, body='{"ok": true}')
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            await executor.execute(
                url="https://api.example.com/events",
                method="PUT",
                headers={},
                payload={"test": "data"},
                timeout=30,
            )

            mock_session.request.assert_called_once()
            call_args = mock_session.request.call_args
            assert call_args[1]["method"] == "PUT"
            assert call_args[1]["url"] == "https://api.example.com/events"
            assert call_args[1]["allow_redirects"] is False

    async def test_executor_sends_headers_from_config(self, executor: WebhookExecutor) -> None:
        """Executor should send configured headers including interpolated values."""
        mock_response = create_mock_response(status=200)
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={
                    "Authorization": "Bearer test-token",
                    "X-Custom-Header": "custom-value",
                },
                payload=None,
                timeout=30,
            )

            call_args = mock_session.request.call_args
            headers = call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer test-token"
            assert headers["X-Custom-Header"] == "custom-value"

    async def test_executor_interpolates_payload_variables(
        self, executor: WebhookExecutor, mock_template_engine: MagicMock
    ) -> None:
        """Executor should interpolate ${context.var} in payload."""
        mock_response = create_mock_response(status=200)
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={},
                payload={"event": "session_end", "id": "${session_id}"},
                timeout=30,
                context={"session_id": "sess-123"},
            )

            assert isinstance(result, WebhookResult)
            assert result.success is True

    async def test_executor_captures_response(self, executor: WebhookExecutor) -> None:
        """Executor should capture status, body, and headers from response."""
        mock_response = create_mock_response(
            status=201,
            body='{"ticket_id": "PROJ-123"}',
            headers={"X-Request-Id": "req-abc"},
        )
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={},
                payload={},
                timeout=30,
            )

            assert result.status_code == 201
            assert result.body == '{"ticket_id": "PROJ-123"}'
            assert result.headers is not None
            assert result.headers["X-Request-Id"] == "req-abc"


class TestWebhookExecutorFailureHandling:
    """Tests for failure handling and retries."""

    async def test_request_timeout_raises_error(self, executor: WebhookExecutor) -> None:
        """Request timeout should raise TimeoutError after configured seconds."""
        mock_session = MagicMock()
        mock_session.request = MagicMock(side_effect=TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={},
                payload={},
                timeout=5,
            )

            assert result.success is False
            assert result.error is not None
            assert "timeout" in result.error.lower()

    async def test_http_5xx_triggers_retry(self, executor: WebhookExecutor) -> None:
        """HTTP 5xx response should trigger retry when in retry_on_status."""
        # Create responses: 500, 500, 200
        responses = [
            create_mock_response(status=500, body="Internal Server Error"),
            create_mock_response(status=500, body="Internal Server Error"),
            create_mock_response(status=200, body='{"ok": true}'),
        ]
        mock_session = create_mock_session(responses)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={},
                payload={},
                timeout=30,
                retry_config={
                    "max_attempts": 3,
                    "backoff_seconds": 0.01,
                    "retry_on_status": [500, 502],
                },
            )

            assert mock_session.request.call_count == 3
            assert result.success is True

    async def test_retry_attempts_reuse_one_pinned_session(self, executor: WebhookExecutor) -> None:
        """All attempts in one execution should share one bounded, DNS-pinned session."""
        responses = [
            create_mock_response(status=500),
            create_mock_response(status=500),
            create_mock_response(status=200),
        ]
        mock_session = create_mock_session(responses)
        connector = MagicMock()

        with (
            patch(
                "gobby.workflows.webhook_executor.aiohttp.ClientSession",
                return_value=mock_session,
            ) as session_factory,
            patch(
                "gobby.workflows.webhook_executor.aiohttp.TCPConnector",
                return_value=connector,
            ) as connector_factory,
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                timeout=17,
                retry_config={"max_attempts": 3, "backoff_seconds": 0, "retry_on_status": [500]},
            )

        assert result.success is True
        session_factory.assert_called_once()
        connector_factory.assert_called_once()
        assert session_factory.call_args.kwargs["connector"] is connector
        assert session_factory.call_args.kwargs["timeout"].total == 17
        resolver = connector_factory.call_args.kwargs["resolver"]
        assert resolver._hostname == "api.example.com"
        assert resolver._addresses[0]["host"] == "93.184.216.34"
        assert mock_session.request.call_count == 3
        assert all(
            call.kwargs["allow_redirects"] is False for call in mock_session.request.call_args_list
        )
        mock_session.__aexit__.assert_awaited_once()

    async def test_session_closes_when_retries_are_exhausted(
        self, executor: WebhookExecutor
    ) -> None:
        """The retry session should close after the final failed attempt."""
        mock_session = create_mock_session(create_mock_response(status=503))

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                retry_config={"max_attempts": 2, "backoff_seconds": 0, "retry_on_status": [503]},
            )

        assert result.success is False
        mock_session.__aexit__.assert_awaited_once()

    async def test_session_closes_when_execution_is_cancelled(
        self, executor: WebhookExecutor
    ) -> None:
        """Cancellation should propagate after closing the retry session."""
        mock_session = create_mock_session(create_mock_response())
        mock_session.request.side_effect = asyncio.CancelledError

        with (
            patch(
                "gobby.workflows.webhook_executor.aiohttp.ClientSession",
                return_value=mock_session,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await executor.execute(url="https://api.example.com/webhook")

        mock_session.request.assert_called_once()
        mock_session.__aexit__.assert_awaited_once()

    async def test_separate_executions_use_separate_sessions(
        self, executor: WebhookExecutor
    ) -> None:
        """A session should never leak into a later webhook execution."""
        sessions = [
            create_mock_session(create_mock_response(status=200)),
            create_mock_session(create_mock_response(status=200)),
        ]

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", side_effect=sessions
        ) as session_factory:
            await executor.execute(url="https://api.example.com/first")
            await executor.execute(url="https://api.example.com/second")

        assert session_factory.call_count == 2
        for session in sessions:
            session.__aexit__.assert_awaited_once()

    async def test_retries_use_exponential_backoff(self, executor: WebhookExecutor) -> None:
        """Retries should use exponential backoff (backoff_seconds * 2^attempt)."""
        call_times = []

        def track_calls(*args: object, **kwargs: object) -> MagicMock:
            call_times.append(asyncio.get_event_loop().time())
            return create_mock_response(status=503, body="Service Unavailable")

        mock_session = MagicMock()
        mock_session.request = MagicMock(side_effect=track_calls)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={},
                payload={},
                timeout=30,
                retry_config={"max_attempts": 3, "backoff_seconds": 0.1, "retry_on_status": [503]},
            )

            # Should have made 3 attempts
            assert len(call_times) == 3
            # Verify exponential backoff: first retry ~0.1s, second ~0.2s
            if len(call_times) >= 2:
                first_delay = call_times[1] - call_times[0]
                assert first_delay >= 0.05  # Allow some timing variance
            if len(call_times) >= 3:
                second_delay = call_times[2] - call_times[1]
                assert second_delay >= first_delay  # Second delay should be longer

    async def test_retry_backoff_is_capped(self, executor: WebhookExecutor) -> None:
        """Exponential retry delays should never exceed the configured safety cap."""
        failure = WebhookResult(success=False, status_code=503, error="HTTP 503")
        request = AsyncMock(return_value=failure)
        with (
            patch.object(executor, "_make_request", new=request),
            patch("gobby.workflows.webhook_executor.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                retry_config={
                    "max_attempts": 3,
                    "backoff_seconds": 60,
                    "retry_on_status": [503],
                },
            )

        assert result == failure
        assert request.await_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [60, 60]

    async def test_max_attempts_exhausted_calls_on_failure(self, executor: WebhookExecutor) -> None:
        """After max_attempts exhausted, on_failure handler should be called."""
        mock_response = create_mock_response(status=500, body="Internal Server Error")
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            on_failure_called = False

            async def on_failure_handler(result: WebhookResult) -> None:
                nonlocal on_failure_called
                on_failure_called = True

            result = await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={},
                payload={},
                timeout=30,
                retry_config={"max_attempts": 2, "backoff_seconds": 0.01, "retry_on_status": [500]},
                on_failure=on_failure_handler,
            )

            assert result.success is False
            assert on_failure_called is True

    async def test_network_error_triggers_retry(self, executor: WebhookExecutor) -> None:
        """Network errors (connection refused) should trigger retry."""
        call_count = [0]

        def mock_request_side_effect(*args: object, **kwargs: object) -> MagicMock:
            call_count[0] += 1
            if call_count[0] < 2:
                raise aiohttp.ClientError("Connection refused")
            return create_mock_response(status=200)

        mock_session = MagicMock()
        mock_session.request = MagicMock(side_effect=mock_request_side_effect)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={},
                payload={},
                timeout=30,
                retry_config={"max_attempts": 3, "backoff_seconds": 0.01, "retry_on_status": [500]},
            )

            assert call_count[0] == 2
            assert result.success is True


class TestWebhookExecutorEdgeCases:
    """Tests for edge cases and special handling."""

    @pytest.mark.parametrize("method", ["delete", "GET", "Patch", "POST", "put"])
    async def test_supported_methods_are_normalized(
        self, executor: WebhookExecutor, method: str
    ) -> None:
        mock_response = create_mock_response(status=200)
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            await executor.execute(url="https://api.example.com/webhook", method=method)

        assert mock_session.request.call_args.kwargs["method"] == method.upper()

    @pytest.mark.parametrize("method", ["", "CONNECT", "OPTIONS", "TRACE"])
    async def test_unsupported_methods_are_rejected_before_network_io(
        self, executor: WebhookExecutor, method: str
    ) -> None:
        with (
            patch("gobby.workflows.webhook_executor.aiohttp.ClientSession") as client_session,
            pytest.raises(ValueError, match="Unsupported webhook method"),
        ):
            await executor.execute(url="https://api.example.com/webhook", method=method)

        client_session.assert_not_called()

    @pytest.mark.parametrize(
        "headers",
        [
            {"Bad Header": "value"},
            {"X-Test": "value\r\ninjected: true"},
        ],
    )
    async def test_invalid_headers_are_rejected_before_network_io(
        self, executor: WebhookExecutor, headers: dict[str, str]
    ) -> None:
        with (
            patch("gobby.workflows.webhook_executor.aiohttp.ClientSession") as client_session,
            pytest.raises(ValueError, match="Invalid webhook header"),
        ):
            await executor.execute(url="https://api.example.com/webhook", headers=headers)

        client_session.assert_not_called()

    @pytest.mark.parametrize(
        "address",
        ["10.0.0.1", "127.0.0.1", "169.254.169.254", "::1"],
    )
    async def test_non_public_resolved_addresses_are_rejected(
        self, executor: WebhookExecutor, address: str
    ) -> None:
        """DNS results must not permit private, loopback, link-local, or metadata targets."""
        resolved = {
            "hostname": "unsafe.example",
            "host": address,
            "port": 443,
            "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
            "proto": 0,
            "flags": 0,
        }
        with (
            patch(
                "gobby.workflows.webhook_executor.DefaultResolver.resolve",
                new=AsyncMock(return_value=[resolved]),
            ),
            patch("gobby.workflows.webhook_executor.aiohttp.ClientSession") as client_session,
            pytest.raises(ValueError, match="non-public address"),
        ):
            await executor.execute(url="https://unsafe.example/webhook")

        client_session.assert_not_called()

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/hook", "https://"])
    async def test_invalid_webhook_urls_are_rejected(
        self, executor: WebhookExecutor, url: str
    ) -> None:
        """Webhook URLs must use HTTP(S) and include a hostname."""
        with pytest.raises(ValueError, match="http or https"):
            await executor.execute(url=url)

    def test_retry_config_uses_canonical_bounds(self, executor: WebhookExecutor) -> None:
        """Executor retry parsing should enforce shared attempt and backoff limits."""
        with pytest.raises(ValueError, match="max_attempts"):
            executor._parse_retry_config({"max_attempts": 100_000})
        with pytest.raises(ValueError, match="backoff_seconds"):
            executor._parse_retry_config({"backoff_seconds": 61})

    async def test_webhook_id_resolves_to_url(
        self,
        executor: WebhookExecutor,
        mock_webhook_registry: dict[str, dict[str, Any]],
    ) -> None:
        """webhook_id should resolve to URL from webhook registry."""
        mock_response = create_mock_response(status=200)
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute_by_webhook_id(
                webhook_id="slack_alerts",
                payload={"text": "Hello"},
            )

            call_args = mock_session.request.call_args
            assert call_args[1]["url"] == "https://hooks.slack.com/services/xxx"
            assert result.success is True

    async def test_missing_webhook_id_raises_error(self, executor: WebhookExecutor) -> None:
        """Missing webhook_id in registry should raise clear error."""
        with pytest.raises(ValueError, match="webhook_id.*not found|unknown webhook"):
            await executor.execute_by_webhook_id(
                webhook_id="nonexistent_webhook",
                payload={},
            )

    async def test_secrets_interpolation_in_headers(
        self, executor: WebhookExecutor, mock_secrets: dict[str, str]
    ) -> None:
        """Secrets interpolation (${secrets.API_KEY}) should work in headers."""
        mock_response = create_mock_response(status=200)
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                method="POST",
                headers={"Authorization": "Bearer ${secrets.API_KEY}"},
                payload={},
                timeout=30,
            )

            call_args = mock_session.request.call_args
            headers = call_args[1]["headers"]
            # Secret should be interpolated
            assert headers["Authorization"] == "Bearer secret-api-key-123"
            assert result.success is True

    async def test_oversized_response_is_typed_failure(self, executor: WebhookExecutor) -> None:
        """An oversized response is rejected without returning a partial body."""
        large_body = "x" * (MAX_RESPONSE_BYTES + 100)
        mock_response = create_mock_response(
            status=200,
            body=large_body,
            headers={"Content-Length": str(len(large_body))},
        )
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://user:secret@api.example.com/webhook?token=secret#fragment",
                method="POST",
                headers={},
                payload={},
                timeout=30,
            )

            assert result.success is False
            assert result.body is None
            assert result.error_code == "response_too_large"
            assert result.error == f"Response body exceeds {MAX_RESPONSE_BYTES} bytes"
            assert result.diagnostics == {
                "captured_bytes": MAX_RESPONSE_BYTES + 1,
                "total_bytes": len(large_body),
                "url": "https://api.example.com/webhook",
            }
            mock_response.content.readexactly.assert_awaited_once_with(MAX_RESPONSE_BYTES + 1)

    async def test_oversized_encoded_response_has_no_total_bytes(
        self, executor: WebhookExecutor
    ) -> None:
        """Wire Content-Length is not used for decompressed response byte accounting."""
        mock_response = create_mock_response(
            status=200,
            body="x" * (MAX_RESPONSE_BYTES + 1),
            headers={"Content-Encoding": "gzip", "Content-Length": "1052"},
        )
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(url="https://api.example.com/webhook")

        assert result.success is False
        assert result.error_code == "response_too_large"
        assert result.diagnostics == {
            "captured_bytes": MAX_RESPONSE_BYTES + 1,
            "total_bytes": None,
            "url": "https://api.example.com/webhook",
        }

    async def test_oversized_response_is_not_retried(self, executor: WebhookExecutor) -> None:
        """A response-size failure is terminal even for a retryable HTTP status."""
        mock_response = create_mock_response(
            status=503,
            body="x" * (MAX_RESPONSE_BYTES + 1),
            headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
        )
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(
                url="https://api.example.com/webhook",
                retry_config={"max_attempts": 3, "retry_on_status": [503]},
            )

        assert result.error_code == "response_too_large"
        assert mock_session.request.call_count == 1

    async def test_exact_limit_response_succeeds(self, executor: WebhookExecutor) -> None:
        """A response at the byte limit remains a complete success."""
        body = "x" * MAX_RESPONSE_BYTES
        mock_response = create_mock_response(
            status=200,
            body=body,
            headers={"Content-Length": str(len(body))},
        )
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute(url="https://api.example.com/webhook")

        assert result.success is True
        assert result.body == body
        assert result.error_code is None
        assert result.diagnostics is None

    async def test_oversized_response_by_webhook_id_includes_id(
        self, executor: WebhookExecutor
    ) -> None:
        """Registry execution failures identify the configured webhook."""
        mock_response = create_mock_response(
            status=200,
            body="x" * (MAX_RESPONSE_BYTES + 1),
            headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
        )
        mock_session = create_mock_session(mock_response)

        with patch(
            "gobby.workflows.webhook_executor.aiohttp.ClientSession", return_value=mock_session
        ):
            result = await executor.execute_by_webhook_id("slack_alerts")

        assert result.success is False
        assert result.error_code == "response_too_large"
        assert result.diagnostics is not None
        assert result.diagnostics["webhook_id"] == "slack_alerts"


class TestWebhookResult:
    """Tests for WebhookResult data class."""

    def test_webhook_result_success_attributes(self) -> None:
        """WebhookResult should have success, status_code, body, headers, error."""
        result = WebhookResult(
            success=True,
            status_code=200,
            body='{"ok": true}',
            headers={"Content-Type": "application/json"},
            error=None,
        )

        assert result.success is True
        assert result.status_code == 200
        assert result.body == '{"ok": true}'
        assert result.headers is not None
        assert result.headers["Content-Type"] == "application/json"
        assert result.error is None

    def test_webhook_result_failure_attributes(self) -> None:
        """WebhookResult for failure should have error message."""
        result = WebhookResult(
            success=False,
            status_code=None,
            body=None,
            headers=None,
            error="Connection refused",
        )

        assert result.success is False
        assert result.status_code is None
        assert result.error == "Connection refused"

    def test_webhook_result_json_body(self) -> None:
        """WebhookResult should have helper to parse JSON body."""
        result = WebhookResult(
            success=True,
            status_code=200,
            body='{"ticket_id": "PROJ-123", "url": "https://jira.example.com/PROJ-123"}',
            headers={},
            error=None,
        )

        json_body = result.json_body()
        assert json_body is not None
        assert json_body["ticket_id"] == "PROJ-123"
        assert json_body["url"] == "https://jira.example.com/PROJ-123"

    def test_webhook_result_json_body_returns_none_for_invalid(self) -> None:
        """json_body() should return None for non-JSON body."""
        result = WebhookResult(
            success=True,
            status_code=200,
            body="Not JSON content",
            headers={},
            error=None,
        )

        assert result.json_body() is None
