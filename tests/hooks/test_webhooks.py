"""Tests for the webhook dispatcher."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.config.extensions import WebhookEndpointConfig, WebhooksConfig
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.webhooks import WebhookDispatcher, WebhookResult

pytestmark = pytest.mark.unit


@pytest.fixture
def sample_event() -> HookEvent:
    """Create a sample hook event for testing."""
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session-123",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={"test": "data"},
        machine_id="machine-1",
        cwd="/test/path",
    )


@pytest.fixture
def basic_endpoint() -> WebhookEndpointConfig:
    """Create a basic webhook endpoint config."""
    return WebhookEndpointConfig(
        name="test-webhook",
        url="https://example.com/webhook",
        events=["session_start"],
        timeout=5.0,
        retry_count=2,
        retry_delay=0.1,
    )


@pytest.fixture
def blocking_endpoint() -> WebhookEndpointConfig:
    """Create a blocking webhook endpoint config."""
    return WebhookEndpointConfig(
        name="blocking-webhook",
        url="https://example.com/blocking",
        events=["before_tool"],
        can_block=True,
        timeout=5.0,
        retry_count=0,
    )


class TestWebhookEndpointConfig:
    """Tests for WebhookEndpointConfig."""

    def test_default_values(self) -> None:
        """Test default endpoint config values."""
        config = WebhookEndpointConfig(
            name="test",
            url="https://example.com/hook",
        )
        assert config.timeout == 10.0
        assert config.retry_count == 3
        assert config.retry_delay == 1.0
        assert config.can_block is False
        assert config.fail_closed is False
        assert config.enabled is True
        assert config.events == []
        assert config.headers == {}

    def test_custom_values(self) -> None:
        """Test custom endpoint config values."""
        config = WebhookEndpointConfig(
            name="custom",
            url="https://example.com/hook",
            events=["session_start", "session_end"],
            headers={"Authorization": "Bearer token"},
            timeout=30.0,
            retry_count=5,
            retry_delay=2.0,
            can_block=True,
            fail_closed=True,
        )
        assert config.timeout == 30.0
        assert config.retry_count == 5
        assert config.can_block is True
        assert config.fail_closed is True
        assert "session_start" in config.events


class TestWebhooksConfig:
    """Tests for WebhooksConfig."""

    def test_default_values(self) -> None:
        """Test default webhooks config values."""
        config = WebhooksConfig()
        assert config.enabled is True
        assert config.endpoints == []
        assert config.default_timeout == 10.0
        assert config.async_dispatch is True

    def test_with_endpoints(self) -> None:
        """Test config with multiple endpoints."""
        config = WebhooksConfig(
            endpoints=[
                WebhookEndpointConfig(name="ep1", url="https://example.com/1"),
                WebhookEndpointConfig(name="ep2", url="https://example.com/2"),
            ]
        )
        assert len(config.endpoints) == 2


class TestWebhookDispatcherMatching:
    """Tests for webhook endpoint matching logic."""

    def test_matches_exact_event(self, basic_endpoint: WebhookEndpointConfig) -> None:
        """Test matching exact event type."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        assert dispatcher._matches_event(basic_endpoint, "session_start") is True
        assert dispatcher._matches_event(basic_endpoint, "session_end") is False

    def test_matches_kebab_case(self, basic_endpoint: WebhookEndpointConfig) -> None:
        """Test matching with kebab-case event type."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        # Should match kebab-case variant
        assert dispatcher._matches_event(basic_endpoint, "session-start") is True

    def test_matches_uppercase(self, basic_endpoint: WebhookEndpointConfig) -> None:
        """Test matching with uppercase event type."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        assert dispatcher._matches_event(basic_endpoint, "SESSION_START") is True

    def test_empty_events_matches_all(self) -> None:
        """Test that empty events list matches all events."""
        endpoint = WebhookEndpointConfig(
            name="catch-all",
            url="https://example.com/all",
            events=[],  # Empty = all events
        )
        config = WebhooksConfig(endpoints=[endpoint])
        dispatcher = WebhookDispatcher(config)

        assert dispatcher._matches_event(endpoint, "session_start") is True
        assert dispatcher._matches_event(endpoint, "before_tool") is True
        assert dispatcher._matches_event(endpoint, "anything") is True


class TestWebhookDispatcherPayload:
    """Tests for payload building."""

    def test_build_payload(self, sample_event: HookEvent) -> None:
        """Test building webhook payload from event."""
        config = WebhooksConfig()
        dispatcher = WebhookDispatcher(config)

        payload = dispatcher._build_payload(sample_event)

        assert payload["event_type"] == "session_start"
        assert payload["session_id"] == "test-session-123"
        assert payload["source"] == "claude"
        assert payload["data"] == {"test": "data"}
        assert payload["machine_id"] == "machine-1"
        assert payload["cwd"] == "/test/path"

    def test_build_payload_includes_enriched_block_response(self, sample_event: HookEvent) -> None:
        dispatcher = WebhookDispatcher(WebhooksConfig())
        response = HookResponse(
            decision="block",
            reason="Rule denied",
            metadata={"session_ref": "#42", "enriched": True},
        )

        payload = dispatcher._build_payload(sample_event, response)

        assert payload["response"]["decision"] == "block"
        assert payload["response"]["reason"] == "Rule denied"
        assert payload["response"]["metadata"] == {
            "session_ref": "#42",
            "enriched": True,
        }


class TestWebhookDispatcherTrigger:
    """Tests for webhook triggering."""

    @pytest.mark.asyncio
    async def test_trigger_disabled(self, sample_event: HookEvent):
        """Test that disabled config returns empty results."""
        config = WebhooksConfig(enabled=False)
        dispatcher = WebhookDispatcher(config)

        results = await dispatcher.trigger(sample_event)

        assert results == []

    @pytest.mark.asyncio
    async def test_trigger_no_matching_endpoints(self, sample_event: HookEvent):
        """Test triggering with no matching endpoints."""
        endpoint = WebhookEndpointConfig(
            name="wrong-event",
            url="https://example.com/hook",
            events=["before_tool"],  # Won't match session_start
        )
        config = WebhooksConfig(endpoints=[endpoint])
        dispatcher = WebhookDispatcher(config)

        results = await dispatcher.trigger(sample_event)

        assert results == []

    @pytest.mark.asyncio
    async def test_trigger_success(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ):
        """Test successful webhook dispatch."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(
            200,
            json={"status": "ok"},
        )

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is True
            assert results[0].status_code == 200
            assert results[0].endpoint_name == "test-webhook"

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_trigger_expands_environment_in_url_and_headers(
        self, sample_event: HookEvent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Expand URL/header variables while preserving unresolved placeholders."""
        monkeypatch.setenv("WEBHOOK_HOST", "hooks.example.com")
        monkeypatch.setenv("WEBHOOK_TOKEN", "secret-token")
        monkeypatch.delenv("WEBHOOK_UNSET", raising=False)
        endpoint = WebhookEndpointConfig(
            name="expanded-webhook",
            url="https://${WEBHOOK_HOST}/hook",
            events=["session_start"],
            headers={
                "Authorization": "Bearer ${WEBHOOK_TOKEN}",
                "X-Unresolved": "${WEBHOOK_UNSET}",
            },
        )
        dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[endpoint]))

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(200, json={"status": "ok"})

            results = await dispatcher.trigger(sample_event)

        assert results[0].success is True
        mock_post.assert_awaited_once()
        call = mock_post.await_args
        assert call.args[0] == "https://hooks.example.com/hook"
        assert call.kwargs["headers"]["Authorization"] == "Bearer secret-token"
        assert call.kwargs["headers"]["X-Unresolved"] == "${WEBHOOK_UNSET}"
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_trigger_client_error_no_retry(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ):
        """Test that 4xx errors don't trigger retries."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(400, json={"error": "bad request"})

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is False
            assert results[0].status_code == 400
            assert results[0].attempts == 1  # No retries

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_trigger_server_error_with_retry(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ):
        """Test that 5xx errors trigger retries."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(500, json={"error": "server error"})

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is False
            assert results[0].attempts == 3  # Initial + 2 retries

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_trigger_timeout_with_retry(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ):
        """Test that timeouts trigger retries."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.TimeoutException("timeout")

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is False
            assert results[0].error == "Request timeout"
            assert results[0].attempts == 3

        await dispatcher.close()


class TestBlockingWebhooks:
    """Tests for blocking webhook functionality."""

    @pytest.mark.asyncio
    async def test_blocking_webhook_allow(self, blocking_endpoint: WebhookEndpointConfig):
        """Test blocking webhook that allows action."""
        blocking_endpoint = blocking_endpoint.model_copy(update={"fail_closed": True})
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool": "bash"},
        )
        config = WebhooksConfig(endpoints=[blocking_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(
            200,
            json={"decision": "allow"},
        )

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(event)

            assert len(results) == 1
            assert results[0].decision == "allow"

            decision, reason = dispatcher.get_blocking_decision(results)
            assert decision == "allow"
            assert reason is None

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_blocking_webhook_block(self, blocking_endpoint: WebhookEndpointConfig):
        """Test blocking webhook that blocks action."""
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool": "bash"},
        )
        config = WebhooksConfig(endpoints=[blocking_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(
            200,
            json={"decision": "block", "reason": "Not allowed"},
        )

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(event)

            assert len(results) == 1
            assert results[0].decision == "block"

            decision, reason = dispatcher.get_blocking_decision(results)
            assert decision == "block"
            assert reason == "Not allowed"

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_blocking_webhook_deny(self, blocking_endpoint: WebhookEndpointConfig):
        """Test blocking webhook with deny decision."""
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool": "rm"},
        )
        config = WebhooksConfig(endpoints=[blocking_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(
            200,
            json={"decision": "deny", "reason": "Dangerous command"},
        )

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(event)

            decision, reason = dispatcher.get_blocking_decision(results)
            assert decision == "block"  # deny is treated as block
            assert reason == "Dangerous command"

        await dispatcher.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("can_block", "fail_closed", "expected_decision"),
        [
            (True, False, "allow"),
            (True, True, "block"),
            (False, True, "allow"),
        ],
    )
    @pytest.mark.parametrize(
        "failure_mode",
        ["client_error", "server_error", "timeout", "connection_error"],
    )
    async def test_blocking_webhook_failure_policy(
        self,
        blocking_endpoint: WebhookEndpointConfig,
        can_block: bool,
        fail_closed: bool,
        expected_decision: str,
        failure_mode: str,
    ) -> None:
        """Blocking webhook failures honor the endpoint's explicit failure policy."""
        endpoint = blocking_endpoint.model_copy(
            update={
                "can_block": can_block,
                "fail_closed": fail_closed,
                "retry_count": 1,
                "retry_delay": 0.1,
            }
        )
        dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[endpoint]))
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"tool": "bash"},
        )

        with (
            patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post,
            patch("gobby.hooks.webhooks.asyncio.sleep", new_callable=AsyncMock),
        ):
            if failure_mode == "client_error":
                mock_post.return_value = httpx.Response(
                    400,
                    json={"decision": "block", "error": "invalid"},
                )
            elif failure_mode == "server_error":
                mock_post.return_value = httpx.Response(503, json={"error": "unavailable"})
            elif failure_mode == "timeout":
                mock_post.side_effect = httpx.TimeoutException("timeout")
            else:
                mock_post.side_effect = httpx.ConnectError("offline")

            results = await dispatcher.trigger(event)

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].attempts == (1 if failure_mode == "client_error" else 2)
        assert dispatcher.get_blocking_decision(results)[0] == expected_decision

        await dispatcher.close()


class TestWebhookResult:
    """Tests for WebhookResult dataclass."""

    def test_success_result(self) -> None:
        """Test creating a success result."""
        result = WebhookResult(
            endpoint_name="test",
            success=True,
            status_code=200,
            response_body={"ok": True},
            attempts=1,
            duration_ms=50.5,
        )
        assert result.success is True
        assert result.error is None

    def test_failure_result(self) -> None:
        """Test creating a failure result."""
        result = WebhookResult(
            endpoint_name="test",
            success=False,
            error="Connection refused",
            attempts=3,
            duration_ms=5000.0,
        )
        assert result.success is False
        assert result.status_code is None
        assert result.error == "Connection refused"
