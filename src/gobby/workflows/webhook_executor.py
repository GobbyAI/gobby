"""
Webhook action executor for workflows.

Executes HTTP requests as workflow actions with retry logic,
variable interpolation, and response capture.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from gobby.workflows.templates import TemplateRenderer

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from gobby.workflows.webhook import MAX_RETRY_BACKOFF_SECONDS, RetryConfig

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 1024 * 1024
ALLOWED_METHODS = frozenset({"DELETE", "GET", "PATCH", "POST", "PUT"})
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class WebhookFailureDiagnostics(TypedDict):
    """Structured context for a typed webhook execution failure."""

    captured_bytes: int
    total_bytes: int | None
    url: str
    webhook_id: NotRequired[str]


@dataclass
class WebhookResult:
    """Result of a webhook execution."""

    success: bool
    status_code: int | None = None
    body: str | None = None
    headers: dict[str, str] | None = None
    error: str | None = None
    error_code: Literal["response_too_large"] | None = None
    diagnostics: WebhookFailureDiagnostics | None = None

    def json_body(self) -> dict[str, Any] | None:
        """Parse body as JSON.

        Returns:
            Parsed JSON dict, or None if body is not valid JSON.
        """
        if not self.body:
            return None
        try:
            result: dict[str, Any] = json.loads(self.body)
            return result
        except json.JSONDecodeError:
            return None


class _PinnedResolver(AbstractResolver):
    """Resolve one hostname to a previously validated set of public addresses."""

    def __init__(self, hostname: str, addresses: list[ResolveResult]) -> None:
        self._hostname = hostname.rstrip(".").lower()
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if host.rstrip(".").lower() != self._hostname:
            raise OSError(f"Unexpected webhook hostname: {host}")
        return self._addresses.copy()

    async def close(self) -> None:
        return None


class WebhookExecutor:
    """Executes webhook HTTP requests from workflows.

    Handles URL resolution, variable interpolation, retries,
    and response capture.
    """

    def __init__(
        self,
        template_engine: TemplateRenderer | None = None,
        webhook_registry: dict[str, dict[str, Any]] | None = None,
        secrets: dict[str, str] | None = None,
    ):
        """Initialize the executor.

        Args:
            template_engine: Optional template engine for variable interpolation.
            webhook_registry: Dict mapping webhook_id to config (url, headers, etc.).
            secrets: Dict of secret values for ${secrets.VAR} interpolation.
        """
        self.template_engine = template_engine
        self.webhook_registry = webhook_registry or {}
        self.secrets = secrets or {}

    async def execute(
        self,
        url: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | str | None = None,
        timeout: int = 30,
        retry_config: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        on_success: Callable[[WebhookResult], Coroutine[Any, Any, None]] | None = None,
        on_failure: Callable[[WebhookResult], Coroutine[Any, Any, None]] | None = None,
    ) -> WebhookResult:
        """Execute a webhook HTTP request.

        Args:
            url: Target URL for the request.
            method: HTTP method (GET, POST, PUT, PATCH, DELETE).
            headers: Request headers (supports ${secrets.VAR} interpolation).
            payload: Request body as dict or string.
            timeout: Request timeout in seconds.
            retry_config: Optional retry configuration dict.
            context: Context dict for variable interpolation.
            on_success: Async callback for successful (2xx) response.
            on_failure: Async callback after all retries exhausted.

        Returns:
            WebhookResult with response data or error.
        """
        headers = headers or {}
        context = context or {}

        normalized_method = self._validate_method(method)

        # Interpolate secrets in headers
        interpolated_headers = self._interpolate_secrets(headers)
        self._validate_headers(interpolated_headers)

        # Interpolate context in payload
        interpolated_payload = self._interpolate_payload(payload, context)

        # Parse retry config
        retry = self._parse_retry_config(retry_config)

        # Execute with retry logic
        result = await self._execute_with_retry(
            url=url,
            method=normalized_method,
            headers=interpolated_headers,
            payload=interpolated_payload,
            timeout=timeout,
            retry=retry,
        )

        # Call appropriate handler
        if result.success and on_success:
            await on_success(result)
        elif not result.success and on_failure:
            await on_failure(result)

        return result

    @staticmethod
    def _validate_method(method: str) -> str:
        """Return a normalized supported HTTP method."""
        if not isinstance(method, str) or method.upper() not in ALLOWED_METHODS:
            supported = ", ".join(sorted(ALLOWED_METHODS))
            raise ValueError(f"Unsupported webhook method {method!r}; expected one of {supported}")
        return method.upper()

    @staticmethod
    def _validate_headers(headers: dict[str, str]) -> None:
        """Reject malformed header names and values before network I/O."""
        for name, value in headers.items():
            if not isinstance(name, str) or HEADER_NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(f"Invalid webhook header name: {name!r}")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise ValueError(f"Invalid webhook header value for {name!r}")

    async def execute_by_webhook_id(
        self,
        webhook_id: str,
        payload: dict[str, Any] | str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        context: dict[str, Any] | None = None,
        retry_config: dict[str, Any] | None = None,
        on_success: Callable[[WebhookResult], Coroutine[Any, Any, None]] | None = None,
        on_failure: Callable[[WebhookResult], Coroutine[Any, Any, None]] | None = None,
    ) -> WebhookResult:
        """Execute a webhook by looking up its ID in the registry.

        Args:
            webhook_id: ID of the webhook in the registry.
            payload: Request body.
            method: Override HTTP method from registry.
            headers: Additional headers (merged with registry headers).
            timeout: Override timeout from registry.
            context: Context for variable interpolation.
            retry_config: Optional retry configuration dict.
            on_success: Async callback for successful (2xx) response.
            on_failure: Async callback after all retries exhausted.

        Returns:
            WebhookResult with response data or error.

        Raises:
            ValueError: If webhook_id not found in registry.
        """
        if webhook_id not in self.webhook_registry:
            raise ValueError(f"webhook_id '{webhook_id}' not found in registry")

        config = self.webhook_registry[webhook_id]
        url = config.get("url")
        if not url:
            raise ValueError(f"webhook_id '{webhook_id}' has no URL configured")

        # Merge headers (registry defaults + provided overrides)
        merged_headers = dict(config.get("headers", {}))
        if headers:
            merged_headers.update(headers)

        result = await self.execute(
            url=url,
            method=method or config.get("method", "POST"),
            headers=merged_headers,
            payload=payload,
            timeout=timeout or config.get("timeout", 30),
            context=context,
            retry_config=retry_config,
            on_success=on_success,
            on_failure=on_failure,
        )
        if result.error_code == "response_too_large" and result.diagnostics is not None:
            result.diagnostics["webhook_id"] = webhook_id
        return result

    def _interpolate_secrets(self, headers: dict[str, str]) -> dict[str, str]:
        """Interpolate ${secrets.VAR} in header values.

        Args:
            headers: Headers dict with potential secret references.

        Returns:
            Headers with secrets interpolated.

        Raises:
            ValueError: If a referenced secret is not found in self.secrets.
        """
        result = {}
        pattern = re.compile(r"\$\{secrets\.(\w+)\}")

        for key, value in headers.items():
            if isinstance(value, str):
                # Find all secret references in the value
                matches = pattern.findall(value)
                for secret_name in matches:
                    if secret_name not in self.secrets:
                        raise ValueError(
                            f"Missing secret '{secret_name}' referenced in header '{key}'"
                        )
                # Replace all secrets with their values
                result[key] = pattern.sub(
                    lambda m: self.secrets[m.group(1)],
                    value,
                )
            else:
                result[key] = value

        return result

    def _interpolate_payload(
        self,
        payload: dict[str, Any] | str | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | str | None:
        """Interpolate context variables in payload.

        Args:
            payload: Payload to interpolate.
            context: Context dict for variable values.

        Returns:
            Interpolated payload.
        """
        if payload is None:
            return None

        if self.template_engine and isinstance(payload, str):
            rendered: str = self.template_engine.render(payload, context)
            return rendered

        # For dicts, we could deep-interpolate, but for now just return as-is
        # since the tests expect the executor to handle the interpolation
        return payload

    def _parse_retry_config(self, config: dict[str, Any] | None) -> RetryConfig:
        """Parse retry configuration from dict.

        Args:
            config: Retry config dict or None.

        Returns:
            RetryConfig instance.
        """
        if not config:
            return RetryConfig(max_attempts=1)  # No retry by default

        return RetryConfig.from_dict(config)

    async def _execute_with_retry(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: dict[str, Any] | str | None,
        timeout: int,
        retry: RetryConfig,
    ) -> WebhookResult:
        """Execute request with retry logic.

        Args:
            url: Target URL.
            method: HTTP method.
            headers: Request headers.
            payload: Request body.
            timeout: Timeout in seconds.
            retry: Retry configuration.

        Returns:
            WebhookResult with response or error.
        """
        last_error: str | None = None
        last_status: int | None = None

        hostname, addresses = await self._resolve_public_host(url)
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(hostname, addresses))

        async with aiohttp.ClientSession(timeout=client_timeout, connector=connector) as session:
            for attempt in range(retry.max_attempts):
                if attempt > 0:
                    # Exponential backoff
                    delay = min(
                        retry.backoff_seconds * (2 ** (attempt - 1)),
                        MAX_RETRY_BACKOFF_SECONDS,
                    )
                    logger.debug(
                        "Webhook retry %s/%s, backoff %ss", attempt + 1, retry.max_attempts, delay
                    )
                    await asyncio.sleep(delay)

                try:
                    start_time = time.time()
                    result = await self._make_request(
                        session=session,
                        url=url,
                        method=method,
                        headers=headers,
                        payload=payload,
                    )
                    elapsed = time.time() - start_time
                    logger.debug(
                        "Webhook %s %s -> %s (%.2fs)", method, url, result.status_code, elapsed
                    )

                    if result.success:
                        return result

                    if result.error_code == "response_too_large":
                        return result

                    # Check if we should retry
                    if result.status_code and result.status_code in retry.retry_on_status:
                        last_error = f"HTTP {result.status_code}"
                        last_status = result.status_code
                        continue  # Retry

                    # Non-retryable error
                    return result

                except TimeoutError:
                    last_error = f"Timeout after {timeout}s"
                    logger.debug("Webhook timeout: %s", url)
                    continue  # Retry on timeout

                except aiohttp.ClientError as e:
                    last_error = str(e)
                    logger.debug("Webhook connection error: %s - %s", url, e)
                    continue  # Retry on aiohttp client errors

        # All retries exhausted
        return WebhookResult(
            success=False,
            status_code=last_status,
            body=None,
            headers=None,
            error=last_error or "Unknown error",
        )

    async def _make_request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: dict[str, Any] | str | None,
    ) -> WebhookResult:
        """Make a single HTTP request.

        Args:
            session: Session scoped to the retry envelope.
            url: Target URL.
            method: HTTP method.
            headers: Request headers.
            payload: Request body.

        Returns:
            WebhookResult with response data.
        """
        # Prepare request kwargs
        kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers,
            "allow_redirects": False,
        }

        # Add payload
        if payload is not None:
            if isinstance(payload, dict):
                kwargs["json"] = payload
            else:
                kwargs["data"] = payload

        async with session.request(**kwargs) as response:
            # Convert headers to dict
            response_headers = dict(response.headers)

            try:
                body = await self._read_response_body(response)
            except _ResponseTooLargeError as exc:
                return WebhookResult(
                    success=False,
                    status_code=response.status,
                    body=None,
                    headers=response_headers,
                    error=str(exc),
                    error_code="response_too_large",
                    diagnostics={
                        "captured_bytes": exc.captured_bytes,
                        "total_bytes": exc.total_bytes,
                        "url": _sanitize_url(url),
                    },
                )

            success = 200 <= response.status < 300

            return WebhookResult(
                success=success,
                status_code=response.status,
                body=body,
                headers=response_headers,
                error=None if success else f"HTTP {response.status}",
            )

    async def _resolve_public_host(self, url: str) -> tuple[str, list[ResolveResult]]:
        """Resolve an HTTP URL and reject every non-public destination address."""
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Webhook URL must use http or https and include a hostname")

        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError("Webhook URL has an invalid port") from exc

        resolver = DefaultResolver()
        try:
            addresses = await resolver.resolve(parsed.hostname, port, family=socket.AF_UNSPEC)
        finally:
            await resolver.close()

        if not addresses:
            raise ValueError(f"Webhook hostname did not resolve: {parsed.hostname}")

        for address in addresses:
            try:
                resolved_ip = ipaddress.ip_address(address["host"].split("%", 1)[0])
            except ValueError as exc:
                raise ValueError(
                    f"Webhook hostname resolved to an invalid address: {address['host']}"
                ) from exc
            if not resolved_ip.is_global:
                raise ValueError(
                    f"Webhook hostname resolves to a non-public address: {resolved_ip.compressed}"
                )

        return parsed.hostname, addresses

    async def _read_response_body(self, response: aiohttp.ClientResponse) -> str:
        """Read a complete bounded response body or raise a typed overflow."""
        try:
            raw_body = await response.content.readexactly(MAX_RESPONSE_BYTES + 1)
        except asyncio.IncompleteReadError as exc:
            raw_body = exc.partial

        if len(raw_body) > MAX_RESPONSE_BYTES:
            content_encoding = response.headers.get("Content-Encoding", "identity")
            total_bytes = None
            if content_encoding.strip().lower() in {"", "identity"}:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        total_bytes = int(content_length)
                    except ValueError:
                        pass
            raise _ResponseTooLargeError(
                captured_bytes=len(raw_body),
                total_bytes=total_bytes,
            )

        return raw_body.decode(response.get_encoding(), errors="replace")


class _ResponseTooLargeError(Exception):
    """Internal signal carrying representation-consistent overflow metadata."""

    def __init__(self, captured_bytes: int, total_bytes: int | None) -> None:
        super().__init__(f"Response body exceeds {MAX_RESPONSE_BYTES} bytes")
        self.captured_bytes = captured_bytes
        self.total_bytes = total_bytes


def _sanitize_url(url: str) -> str:
    """Remove URL credentials, query parameters, and fragments from diagnostics."""
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
    return parsed._replace(netloc=netloc, query="", fragment="").geturl()
