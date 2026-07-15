"""
Webhook workflow action models.

Defines the WebhookAction class and related configuration models
for making HTTP requests from workflows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# Valid HTTP methods for webhook actions
VALID_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# Default retry status codes (server errors and rate limiting)
DEFAULT_RETRY_STATUS_CODES = [429, 500, 502, 503, 504]
MAX_RETRY_BACKOFF_SECONDS = 60.0


@dataclass
class RetryConfig:
    """Configuration for webhook retry behavior."""

    max_attempts: int = 3
    backoff_seconds: float = 1.0
    retry_on_status: list[int] = field(default_factory=lambda: DEFAULT_RETRY_STATUS_CODES.copy())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetryConfig:
        """Parse RetryConfig from a dict.

        Args:
            data: Dict with retry configuration fields.

        Returns:
            RetryConfig instance.

        Raises:
            ValueError: If max_attempts is outside 1-10 range.
        """
        max_attempts = data.get("max_attempts", 3)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer between 1 and 10")
        if not (1 <= max_attempts <= 10):
            raise ValueError(f"max_attempts must be between 1 and 10, got {max_attempts}")

        backoff_seconds = data.get("backoff_seconds", 1.0)
        if isinstance(backoff_seconds, bool) or not isinstance(backoff_seconds, int | float):
            raise ValueError(f"backoff_seconds must be between 0 and {MAX_RETRY_BACKOFF_SECONDS:g}")
        if not (0 <= backoff_seconds <= MAX_RETRY_BACKOFF_SECONDS):
            raise ValueError(
                f"backoff_seconds must be between 0 and {MAX_RETRY_BACKOFF_SECONDS:g}, "
                f"got {backoff_seconds}"
            )
        retry_on_status = data.get("retry_on_status", DEFAULT_RETRY_STATUS_CODES.copy())

        return cls(
            max_attempts=max_attempts,
            backoff_seconds=float(backoff_seconds),
            retry_on_status=list(retry_on_status),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "retry_on_status": self.retry_on_status,
        }


@dataclass
class CaptureConfig:
    """Configuration for capturing webhook response data."""

    status_var: str | None = None
    body_var: str | None = None
    headers_var: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptureConfig:
        """Parse CaptureConfig from a dict.

        Args:
            data: Dict with capture configuration fields.

        Returns:
            CaptureConfig instance.
        """
        return cls(
            status_var=data.get("status_var"),
            body_var=data.get("body_var"),
            headers_var=data.get("headers_var"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        result: dict[str, Any] = {}
        if self.status_var:
            result["status_var"] = self.status_var
        if self.body_var:
            result["body_var"] = self.body_var
        if self.headers_var:
            result["headers_var"] = self.headers_var
        return result


@dataclass
class WebhookAction:
    """Webhook action definition for workflows.

    Represents an HTTP request that can be made during workflow execution.
    Either `url` or `webhook_id` must be provided, but not both.
    """

    url: str | None = None
    webhook_id: str | None = None
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    payload: str | dict[str, Any] | None = None
    timeout: int = 30
    retry: RetryConfig | None = None
    on_success: str | None = None
    on_failure: str | None = None
    capture_response: CaptureConfig | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebhookAction:
        """Parse WebhookAction from a dict (e.g., from YAML workflow).

        Args:
            data: Dict with webhook action fields.

        Returns:
            WebhookAction instance.

        Raises:
            ValueError: If validation fails (missing url/webhook_id, invalid method, etc.).
        """
        url = data.get("url")
        webhook_id = data.get("webhook_id")

        # Validate url/webhook_id exclusivity
        if url and webhook_id:
            raise ValueError(
                "url and webhook_id are mutually exclusive - provide only one, not both"
            )
        if not url and not webhook_id:
            raise ValueError("Either url or webhook_id is required")

        # Validate URL scheme if url is provided
        if url:
            cls._validate_url(url)

        # Validate method
        method = data.get("method", "POST").upper()
        if method not in VALID_METHODS:
            raise ValueError(
                f"Invalid HTTP method '{method}'. Must be one of: {', '.join(sorted(VALID_METHODS))}"
            )

        # Validate timeout
        timeout = data.get("timeout", 30)
        if not (1 <= timeout <= 300):
            raise ValueError(f"timeout must be in range 1-300, got {timeout}")

        # Parse nested configs
        retry_data = data.get("retry")
        retry = RetryConfig.from_dict(retry_data) if retry_data else None

        capture_data = data.get("capture_response")
        capture = CaptureConfig.from_dict(capture_data) if capture_data else None

        return cls(
            url=url,
            webhook_id=webhook_id,
            method=method,
            headers=data.get("headers", {}),
            payload=data.get("payload"),
            timeout=timeout,
            retry=retry,
            on_success=data.get("on_success"),
            on_failure=data.get("on_failure"),
            capture_response=capture,
        )

    @staticmethod
    def _validate_url(url: str) -> None:
        """Validate URL scheme is http or https.

        Args:
            url: URL string to validate.

        Raises:
            ValueError: If URL scheme is not http or https.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Invalid URL scheme '{parsed.scheme}'. Only http and https are allowed."
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict.

        Returns:
            Dict representation suitable for YAML serialization.
        """
        result: dict[str, Any] = {
            "method": self.method,
            "timeout": self.timeout,
        }

        if self.url:
            result["url"] = self.url
        if self.webhook_id:
            result["webhook_id"] = self.webhook_id
        if self.headers:
            result["headers"] = self.headers
        if self.payload is not None:
            result["payload"] = self.payload
        if self.retry:
            result["retry"] = self.retry.to_dict()
        if self.on_success:
            result["on_success"] = self.on_success
        if self.on_failure:
            result["on_failure"] = self.on_failure
        if self.capture_response:
            result["capture_response"] = self.capture_response.to_dict()

        return result
