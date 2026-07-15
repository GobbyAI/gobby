"""
Tests for WebhookAction model.

TDD Red Phase: These tests should FAIL initially because WebhookAction doesn't exist yet.
"""

import pytest

# This import will fail until WebhookAction is implemented
from gobby.workflows.webhook import CaptureConfig, RetryConfig, WebhookAction

pytestmark = pytest.mark.unit


class TestWebhookActionParsing:
    """Tests for parsing WebhookAction from dict/YAML."""

    def test_parse_minimal_webhook_with_url(self) -> None:
        """Parse webhook with only url (minimal required fields)."""
        data = {
            "url": "https://example.com/webhook",
        }
        action = WebhookAction.from_dict(data)

        assert action.url == "https://example.com/webhook"
        assert action.webhook_id is None
        assert action.method == "POST"  # default
        assert action.timeout == 30  # default

    def test_parse_minimal_webhook_with_webhook_id(self) -> None:
        """Parse webhook with only webhook_id."""
        data = {
            "webhook_id": "slack_alerts",
        }
        action = WebhookAction.from_dict(data)

        assert action.url is None
        assert action.webhook_id == "slack_alerts"
        assert action.method == "POST"

    def test_parse_full_webhook_all_fields(self) -> None:
        """Parse webhook with all fields specified."""
        data = {
            "url": "https://api.example.com/events",
            "method": "PUT",
            "headers": {
                "Authorization": "Bearer ${secrets.API_TOKEN}",
                "X-Custom": "value",
            },
            "payload": {"event": "test", "data": "${context.summary}"},
            "timeout": 60,
            "retry": {
                "max_attempts": 3,
                "backoff_seconds": 2,
                "retry_on_status": [429, 500, 502],
            },
            "on_success": "log_success",
            "on_failure": "alert_failure",
            "capture_response": {
                "status_var": "response_status",
                "body_var": "response_body",
                "headers_var": "response_headers",
            },
        }
        action = WebhookAction.from_dict(data)

        assert action.url == "https://api.example.com/events"
        assert action.method == "PUT"
        assert action.headers["Authorization"] == "Bearer ${secrets.API_TOKEN}"
        assert action.payload == {"event": "test", "data": "${context.summary}"}
        assert action.timeout == 60
        assert action.retry.max_attempts == 3
        assert action.retry.backoff_seconds == 2
        assert action.retry.retry_on_status == [429, 500, 502]
        assert action.on_success == "log_success"
        assert action.on_failure == "alert_failure"
        assert action.capture_response.status_var == "response_status"

    def test_parse_fails_when_both_url_and_webhook_id_provided(self) -> None:
        """Cannot specify both url and webhook_id."""
        data = {
            "url": "https://example.com/webhook",
            "webhook_id": "slack_alerts",
        }
        with pytest.raises(ValueError, match="mutually exclusive|both.*url.*webhook_id"):
            WebhookAction.from_dict(data)

    def test_parse_fails_when_neither_url_nor_webhook_id_provided(self) -> None:
        """Must specify either url or webhook_id."""
        data = {
            "method": "POST",
            "payload": {"test": "data"},
        }
        with pytest.raises(ValueError, match="required|url.*webhook_id"):
            WebhookAction.from_dict(data)

    def test_parse_fails_for_invalid_method(self) -> None:
        """Invalid HTTP method should raise error."""
        data = {
            "url": "https://example.com/webhook",
            "method": "INVALID",
        }
        with pytest.raises(ValueError, match="method|INVALID"):
            WebhookAction.from_dict(data)

    def test_parse_fails_for_timeout_below_minimum(self) -> None:
        """Timeout below 1 second should fail."""
        data = {
            "url": "https://example.com/webhook",
            "timeout": 0,
        }
        with pytest.raises(ValueError, match="timeout|range|1.*300"):
            WebhookAction.from_dict(data)

    def test_parse_fails_for_timeout_above_maximum(self) -> None:
        """Timeout above 300 seconds should fail."""
        data = {
            "url": "https://example.com/webhook",
            "timeout": 500,
        }
        with pytest.raises(ValueError, match="timeout|range|1.*300"):
            WebhookAction.from_dict(data)


class TestWebhookActionURLValidation:
    """Tests for URL validation."""

    def test_url_accepts_https(self) -> None:
        """HTTPS URLs should be accepted."""
        data = {"url": "https://secure.example.com/webhook"}
        action = WebhookAction.from_dict(data)
        assert action.url == "https://secure.example.com/webhook"

    def test_url_accepts_http(self) -> None:
        """HTTP URLs should be accepted."""
        data = {"url": "http://internal.example.com/webhook"}
        action = WebhookAction.from_dict(data)
        assert action.url == "http://internal.example.com/webhook"

    def test_url_rejects_ftp_scheme(self) -> None:
        """FTP URLs should be rejected."""
        data = {"url": "ftp://files.example.com/webhook"}
        with pytest.raises(ValueError, match="http|https|scheme"):
            WebhookAction.from_dict(data)

    def test_url_rejects_file_scheme(self) -> None:
        """File URLs should be rejected."""
        data = {"url": "file:///etc/passwd"}
        with pytest.raises(ValueError, match="http|https|scheme"):
            WebhookAction.from_dict(data)

    def test_url_rejects_javascript_scheme(self) -> None:
        """JavaScript URLs should be rejected."""
        data = {"url": "javascript:alert(1)"}
        with pytest.raises(ValueError, match="http|https|scheme"):
            WebhookAction.from_dict(data)


class TestWebhookActionFieldTypes:
    """Tests for field type validation."""

    def test_headers_accepts_string_values(self) -> None:
        """Headers dict should accept string values."""
        data = {
            "url": "https://example.com/webhook",
            "headers": {
                "Content-Type": "application/json",
                "X-Api-Key": "${secrets.API_KEY}",
            },
        }
        action = WebhookAction.from_dict(data)
        assert action.headers["Content-Type"] == "application/json"
        assert action.headers["X-Api-Key"] == "${secrets.API_KEY}"

    def test_payload_accepts_string(self) -> None:
        """Payload can be a string template."""
        data = {
            "url": "https://example.com/webhook",
            "payload": '{"event": "${event_type}"}',
        }
        action = WebhookAction.from_dict(data)
        assert action.payload == '{"event": "${event_type}"}'

    def test_payload_accepts_dict(self) -> None:
        """Payload can be a dict."""
        data = {
            "url": "https://example.com/webhook",
            "payload": {"event": "test", "nested": {"key": "value"}},
        }
        action = WebhookAction.from_dict(data)
        assert action.payload == {"event": "test", "nested": {"key": "value"}}

    def test_payload_accepts_none(self) -> None:
        """Payload can be None (for GET requests)."""
        data = {
            "url": "https://example.com/webhook",
            "method": "GET",
        }
        action = WebhookAction.from_dict(data)
        assert action.payload is None


class TestWebhookActionSerialization:
    """Tests for serialization back to dict."""

    def test_to_dict_returns_valid_structure(self) -> None:
        """to_dict() should return a dict matching input structure."""
        data = {
            "url": "https://example.com/webhook",
            "method": "POST",
            "headers": {"X-Custom": "value"},
            "payload": {"key": "value"},
            "timeout": 45,
        }
        action = WebhookAction.from_dict(data)
        result = action.to_dict()

        assert result["url"] == "https://example.com/webhook"
        assert result["method"] == "POST"
        assert result["headers"]["X-Custom"] == "value"
        assert result["payload"]["key"] == "value"
        assert result["timeout"] == 45

    def test_to_dict_excludes_none_values(self) -> None:
        """to_dict() should not include None optional fields."""
        data = {"url": "https://example.com/webhook"}
        action = WebhookAction.from_dict(data)
        result = action.to_dict()

        assert "webhook_id" not in result or result.get("webhook_id") is None
        assert "on_success" not in result or result.get("on_success") is None

    def test_round_trip_parse_serialize_parse(self) -> None:
        """Parsing, serializing, and re-parsing should produce identical object."""
        original_data = {
            "url": "https://example.com/webhook",
            "method": "PUT",
            "headers": {"Authorization": "Bearer token"},
            "payload": {"event": "update"},
            "timeout": 60,
            "retry": {
                "max_attempts": 3,
                "backoff_seconds": 2,
                "retry_on_status": [500, 502],
            },
            "capture_response": {
                "status_var": "status",
                "body_var": "body",
            },
        }

        action1 = WebhookAction.from_dict(original_data)
        serialized = action1.to_dict()
        action2 = WebhookAction.from_dict(serialized)

        assert action1.url == action2.url
        assert action1.method == action2.method
        assert action1.headers == action2.headers
        assert action1.payload == action2.payload
        assert action1.timeout == action2.timeout
        assert action1.retry.max_attempts == action2.retry.max_attempts
        assert action1.capture_response.status_var == action2.capture_response.status_var


class TestRetryConfig:
    """Tests for RetryConfig model."""

    def test_retry_config_from_dict(self) -> None:
        """RetryConfig should parse from dict."""
        data = {
            "max_attempts": 5,
            "backoff_seconds": 3,
            "retry_on_status": [429, 500, 502, 503],
        }
        config = RetryConfig.from_dict(data)

        assert config.max_attempts == 5
        assert config.backoff_seconds == 3
        assert config.retry_on_status == [429, 500, 502, 503]

    def test_retry_config_defaults(self) -> None:
        """RetryConfig should have sensible defaults."""
        config = RetryConfig.from_dict({})

        assert config.max_attempts == 3  # default
        assert config.backoff_seconds == 1  # default
        assert 500 in config.retry_on_status  # default includes server errors

    def test_retry_config_max_attempts_validation(self) -> None:
        """max_attempts should be between 1 and 10."""
        with pytest.raises(ValueError, match="max_attempts|1.*10"):
            RetryConfig.from_dict({"max_attempts": 0})

        with pytest.raises(ValueError, match="max_attempts|1.*10"):
            RetryConfig.from_dict({"max_attempts": 15})

    @pytest.mark.parametrize("backoff_seconds", [-1, 61, True, "slow"])
    def test_retry_config_backoff_validation(self, backoff_seconds: object) -> None:
        """backoff_seconds should be numeric and bounded to one minute."""
        with pytest.raises(ValueError, match="backoff_seconds|0.*60"):
            RetryConfig.from_dict({"backoff_seconds": backoff_seconds})


class TestCaptureConfig:
    """Tests for CaptureConfig model."""

    def test_capture_config_from_dict(self) -> None:
        """CaptureConfig should parse from dict."""
        data = {
            "status_var": "webhook_status",
            "body_var": "webhook_body",
            "headers_var": "webhook_headers",
        }
        config = CaptureConfig.from_dict(data)

        assert config.status_var == "webhook_status"
        assert config.body_var == "webhook_body"
        assert config.headers_var == "webhook_headers"

    def test_capture_config_partial(self) -> None:
        """CaptureConfig with only some fields."""
        data = {"status_var": "status"}
        config = CaptureConfig.from_dict(data)

        assert config.status_var == "status"
        assert config.body_var is None
        assert config.headers_var is None
