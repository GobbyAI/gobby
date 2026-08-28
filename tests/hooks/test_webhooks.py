"""Tests for the webhook dispatcher."""

import asyncio
import time
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.config.extensions import WebhookEndpointConfig, WebhooksConfig
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.session_materialize import activate_deferred_session
from gobby.hooks.webhooks import (
    _MAX_WEBHOOK_RESPONSE_BYTES,
    WebhookDispatcher,
    WebhookResult,
)
from gobby.utils.webhook_transport import DEFAULT_MAX_BACKOFF_SECONDS, WebhookTransport

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def public_webhook_dns() -> Iterator[None]:
    """Keep unit tests deterministic while exercising pinned request construction."""
    with patch.object(
        WebhookTransport,
        "_lookup_addresses",
        new=AsyncMock(return_value=("93.184.216.34",)),
    ):
        yield


@pytest.fixture
def sample_event() -> HookEvent:
    """Create a sample hook event for testing."""
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session-123",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={"test": "data"},
        machine_id="21000000-0000-4000-8000-000000000001",
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
        assert payload["machine_id"] == "21000000-0000-4000-8000-000000000001"
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
    async def test_trigger_disabled(self, sample_event: HookEvent) -> None:
        """Test that disabled config returns empty results."""
        config = WebhooksConfig(enabled=False)
        dispatcher = WebhookDispatcher(config)

        results = await dispatcher.trigger(sample_event)

        assert results == []

    @pytest.mark.asyncio
    async def test_trigger_no_matching_endpoints(self, sample_event: HookEvent) -> None:
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
    ) -> None:
        """Test successful webhook dispatch."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(
            200,
            json={"status": "ok"},
        )

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = mock_response

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is True
            assert results[0].status_code == 200
            assert results[0].endpoint_name == "test-webhook"

        await dispatcher.close()

    async def test_oversized_response_is_rejected(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ) -> None:
        dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[basic_endpoint]))
        response = httpx.Response(200, content=b"x" * (_MAX_WEBHOOK_RESPONSE_BYTES + 1))

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = response
            results = await dispatcher.trigger(sample_event)

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].attempts == 1
        assert results[0].error == (f"Response body exceeds {_MAX_WEBHOOK_RESPONSE_BYTES} bytes")
        await dispatcher.close()

    async def test_malformed_json_response_remains_successful(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ) -> None:
        dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[basic_endpoint]))
        response = httpx.Response(200, content=b"not-json")

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = response
            results = await dispatcher.trigger(sample_event)

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].response_body is None
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_redirect_does_not_forward_auth_headers(self, sample_event: HookEvent) -> None:
        requests: list[httpx.Request] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                302,
                headers={"Location": "http://target.internal/collect"},
            )

        endpoint = WebhookEndpointConfig(
            name="redirecting",
            url="https://origin.example/hook",
            headers={"Authorization": "Bearer secret"},
            retry_count=3,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle_request))
        dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[endpoint]))

        with patch("gobby.hooks.webhooks.httpx.AsyncClient", return_value=client) as client_cls:
            results = await dispatcher.trigger(sample_event)

        assert [request.url.host for request in requests] == ["93.184.216.34"]
        assert requests[0].headers["host"] == "origin.example"
        assert requests[0].headers["Authorization"] == "Bearer secret"
        assert results[0].success is False
        assert results[0].attempts == 1
        client_cls.assert_called_once()
        assert client_cls.call_args.kwargs["follow_redirects"] is False
        assert client_cls.call_args.kwargs["trust_env"] is False
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_invalid_url_is_rejected_before_request(
        self,
        sample_event: HookEvent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WEBHOOK_RUNTIME_HOST", "hooks.example.com")
        endpoint = WebhookEndpointConfig(
            name="invalid-url",
            url="https://${WEBHOOK_RUNTIME_HOST}/collect",
            retry_count=0,
        )
        monkeypatch.setenv("WEBHOOK_RUNTIME_HOST", "bad host")
        dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[endpoint]))

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
            results = await dispatcher.trigger(sample_event)

        mock_post.assert_not_awaited()
        assert results[0].success is False
        assert "Invalid webhook URL" in (results[0].error or "")
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_blocking_endpoint_respects_aggregate_deadline(
        self,
        sample_event: HookEvent,
        blocking_endpoint: WebhookEndpointConfig,
    ) -> None:
        blocking_endpoint.events = []
        dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[blocking_endpoint]))

        async def slow_dispatch(*_args: object, **_kwargs: object) -> WebhookResult:
            await asyncio.Event().wait()
            return WebhookResult(endpoint_name="blocking-webhook", success=True)

        with patch.object(dispatcher, "_dispatch_single", side_effect=slow_dispatch):
            started = time.monotonic()
            results = await dispatcher.trigger(
                sample_event,
                deadline=BlockingEffectDeadline(started + 0.02),
            )
            elapsed = time.monotonic() - started

        assert elapsed < 0.5
        assert results[0].success is False
        assert results[0].error == "Aggregate blocking deadline exceeded"

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

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(200, json={"status": "ok"})

            results = await dispatcher.trigger(sample_event)

        assert results[0].success is True
        mock_post.assert_awaited_once()
        call = mock_post.await_args
        assert call is not None
        request = call.args[0]
        assert str(request.url) == "https://93.184.216.34/hook"
        assert request.headers["host"] == "hooks.example.com"
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.headers["X-Unresolved"] == "${WEBHOOK_UNSET}"
        assert call.kwargs["stream"] is True
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_trigger_client_error_no_retry(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ) -> None:
        """Test that 4xx errors don't trigger retries."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(400, json={"error": "bad request"})

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is False
            assert results[0].status_code == 400
            assert results[0].attempts == 1  # No retries

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_trigger_server_error_retries_post(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ) -> None:
        """Configured retries give POST webhooks at-least-once delivery after 5xx."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        mock_response = httpx.Response(500, json={"error": "server error"})

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is False
            assert results[0].attempts == 3
            assert mock_post.await_count == 3

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_trigger_timeout_retries_post(
        self, sample_event: HookEvent, basic_endpoint: WebhookEndpointConfig
    ) -> None:
        """Configured retries give timed-out POST webhooks at-least-once delivery."""
        config = WebhooksConfig(endpoints=[basic_endpoint])
        dispatcher = WebhookDispatcher(config)

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.TimeoutException("timeout")

            results = await dispatcher.trigger(sample_event)

            assert len(results) == 1
            assert results[0].success is False
            assert results[0].error == "Request timeout"
            assert results[0].attempts == 3
            assert mock_post.await_count == 3

        await dispatcher.close()


class TestBlockingWebhooks:
    """Tests for blocking webhook functionality."""

    @pytest.mark.asyncio
    async def test_blocking_webhook_allow(self, blocking_endpoint: WebhookEndpointConfig) -> None:
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

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(event)

            assert len(results) == 1
            assert results[0].decision == "allow"

            decision, reason = dispatcher.get_blocking_decision(results)
            assert decision == "allow"
            assert reason is None

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_blocking_webhook_block(self, blocking_endpoint: WebhookEndpointConfig) -> None:
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

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            results = await dispatcher.trigger(event)

            assert len(results) == 1
            assert results[0].decision == "block"

            decision, reason = dispatcher.get_blocking_decision(results)
            assert decision == "block"
            assert reason == "Not allowed"

        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_blocking_webhook_deny(self, blocking_endpoint: WebhookEndpointConfig) -> None:
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

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post:
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
            patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_post,
            patch("gobby.utils.webhook_transport.asyncio.sleep", new_callable=AsyncMock),
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
        expected_attempts = 1 if failure_mode == "client_error" else 2
        assert results[0].attempts == expected_attempts
        assert dispatcher.get_blocking_decision(results)[0] == expected_decision

        await dispatcher.close()


async def test_dispatcher_allows_and_pins_private_endpoint(
    sample_event: HookEvent,
) -> None:
    endpoint = WebhookEndpointConfig(
        name="local-webhook",
        url="http://localhost:8765/hook",
        events=["session_start"],
        retry_count=0,
    )
    dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[endpoint]))

    with (
        patch.object(
            dispatcher._transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("127.0.0.1",)),
        ),
        patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as send,
    ):
        send.return_value = httpx.Response(204)
        results = await dispatcher.trigger(sample_event)

    assert send.await_args is not None
    request = send.await_args.args[0]
    assert results[0].success is True
    assert request.url.host == "127.0.0.1"
    assert request.headers["host"] == "localhost:8765"
    assert dispatcher._transport.allow_private_addresses is True
    await dispatcher.close()


async def test_dispatcher_retry_backoff_is_capped(
    sample_event: HookEvent,
) -> None:
    endpoint = WebhookEndpointConfig(
        name="offline-webhook",
        url="https://hooks.example/hook",
        events=["session_start"],
        retry_count=3,
        retry_delay=30,
    )
    dispatcher = WebhookDispatcher(WebhooksConfig(endpoints=[endpoint]))

    with (
        patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as send,
        patch("gobby.utils.webhook_transport.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        send.side_effect = httpx.ConnectError("offline")
        results = await dispatcher.trigger(sample_event)

    assert results[0].attempts == 4
    assert [call.args[0] for call in sleep.await_args_list] == [
        30,
        DEFAULT_MAX_BACKOFF_SECONDS,
        DEFAULT_MAX_BACKOFF_SECONDS,
    ]
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


@pytest.mark.parametrize("blocked", [False, True], ids=["allow", "block"])
def test_deferred_start_webhooks_use_synthetic_event_and_gate_live_response(
    blocked: bool,
) -> None:
    manager = MagicMock()
    session = SimpleNamespace(
        id="platform-session",
        seq_num=42,
        project_id="project-1",
        parent_session_id=None,
        transcript_path="/tmp/transcript.jsonl",
    )
    manager._session_manager.get.return_value = session
    manager._event_handlers._activate_materialized_session.return_value = []
    startup_response = HookResponse(
        decision="allow",
        context="startup context",
        system_message="Gobby Session ID: #42",
    )
    manager._event_handlers._compose_session_response.return_value = startup_response
    manager._evaluate_workflow_rules.return_value = (None, None)
    blocking_response = HookResponse(decision="block", reason="webhook blocked startup")
    manager._evaluate_blocking_webhooks.return_value = blocking_response if blocked else None
    manager._complete_response.side_effect = lambda _event, response, *_args, **_kwargs: response
    manager.get_machine_id.return_value = "machine-1"
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="external-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={"prompt": "hello", "cwd": "/tmp"},
        machine_id="machine-1",
        project_id="project-1",
        metadata={"_platform_session_id": "platform-session"},
    )

    result = activate_deferred_session(manager, event, BlockingEffectDeadline(123.0))

    copied = manager._evaluate_blocking_webhooks.call_args.args[0]
    assert copied.event_type is HookEventType.SESSION_START
    assert copied.data == {
        "source": "startup",
        "cwd": "/tmp",
        "terminal_context": {"cwd": "/tmp"},
    }
    assert copied.metadata == {
        "_platform_session_id": "platform-session",
        "_synthetic_session_start": True,
    }
    if blocked:
        assert result is blocking_response
        manager._complete_response.assert_called_once()
    else:
        assert result is None
        manager._dispatch_webhooks_async.assert_called_once_with(copied, startup_response)


def test_sessionless_start_suppresses_rules_and_webhooks() -> None:
    manager = MagicMock()
    manager._session_manager = MagicMock()
    sessionless = HookResponse(decision="allow")
    manager._get_event_handler.return_value = lambda _event: sessionless
    completed = HookResponse(decision="allow")
    manager._complete_response.return_value = completed
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="deferred-start",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={"source": "startup", "cwd": "/tmp"},
    )

    with patch(
        "gobby.hooks.hook_manager.resolve_hook_project_context",
        return_value=SimpleNamespace(skipped=False, project_id="project-1", reason=None),
    ):
        response = HookManager._handle_after_daemon_ready(
            manager,
            event,
            BlockingEffectDeadline(123.0),
        )

    assert response is completed
    manager._evaluate_workflow_rules.assert_not_called()
    manager._evaluate_blocking_webhooks.assert_not_called()
    manager._complete_response.assert_called_once_with(
        event,
        sessionless,
        None,
        preserve_original=True,
        suppress_webhooks=True,
    )
