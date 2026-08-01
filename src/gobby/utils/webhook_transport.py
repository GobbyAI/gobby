"""Shared hardened HTTP transport for outbound webhooks."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from urllib.parse import SplitResult, urlsplit

import httpx

from gobby.utils.http_retry import parse_retry_after
from gobby.utils.url_sanitize import sanitize_url

DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_BACKOFF_SECONDS = 60.0
ALLOWED_METHODS = frozenset({"DELETE", "GET", "PATCH", "POST", "PUT"})
IDEMPOTENT_METHODS = frozenset({"DELETE", "GET", "PUT"})
DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

_PROVABLY_UNSENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


class WebhookFailureDiagnostics(TypedDict):
    """Structured context for a typed webhook execution failure."""

    captured_bytes: int
    total_bytes: int | None
    url: str


@dataclass(slots=True)
class WebhookTransportResult:
    """Result of one logical webhook delivery."""

    success: bool
    status_code: int | None = None
    body: str | None = None
    headers: dict[str, str] | None = None
    error: str | None = None
    error_code: Literal["response_too_large"] | None = None
    diagnostics: WebhookFailureDiagnostics | None = None
    attempts: int = 1

    def json_body(self) -> dict[str, Any] | None:
        """Return a JSON object response, when the body contains one."""
        if not self.body:
            return None
        try:
            parsed = json.loads(self.body)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


class _ResponseTooLargeError(Exception):
    """Internal signal carrying bounded-read overflow metadata."""

    def __init__(self, captured_bytes: int, total_bytes: int | None) -> None:
        self.captured_bytes = captured_bytes
        self.total_bytes = total_bytes
        super().__init__()


class WebhookTransport:
    """Validate, pin, and deliver outbound webhook requests."""

    def __init__(self, *, allow_private_addresses: bool = False) -> None:
        self.allow_private_addresses = allow_private_addresses

    async def execute(
        self,
        url: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | str | bytes | None = None,
        timeout: float = 30,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_attempts: int = 1,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        retry_statuses: Collection[int] = DEFAULT_RETRY_STATUSES,
        client: httpx.AsyncClient | None = None,
    ) -> WebhookTransportResult:
        """Execute a request after validating and pinning every resolved address."""
        normalized_method = self._validate_method(method)
        request_headers = headers or {}
        self._validate_headers(request_headers)
        self._validate_limits(
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )

        parsed, hostname, port = self._parse_url(url)
        addresses = await self._lookup_addresses(hostname, port)
        self._validate_addresses(addresses)

        if client is not None:
            return await self._execute_with_client(
                client=client,
                parsed=parsed,
                hostname=hostname,
                port=port,
                addresses=addresses,
                method=normalized_method,
                headers=request_headers,
                payload=payload,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                max_backoff_seconds=max_backoff_seconds,
                retry_statuses=retry_statuses,
            )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        ) as owned_client:
            return await self._execute_with_client(
                client=owned_client,
                parsed=parsed,
                hostname=hostname,
                port=port,
                addresses=addresses,
                method=normalized_method,
                headers=request_headers,
                payload=payload,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                max_backoff_seconds=max_backoff_seconds,
                retry_statuses=retry_statuses,
            )

    async def _execute_with_client(
        self,
        *,
        client: httpx.AsyncClient,
        parsed: SplitResult,
        hostname: str,
        port: int,
        addresses: tuple[str, ...],
        method: str,
        headers: dict[str, str],
        payload: dict[str, Any] | str | bytes | None,
        timeout: float,
        max_response_bytes: int,
        max_attempts: int,
        backoff_seconds: float,
        max_backoff_seconds: float,
        retry_statuses: Collection[int],
    ) -> WebhookTransportResult:
        retry_after: str | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                delay = self._retry_delay(
                    retry_after=retry_after,
                    attempt=attempt,
                    backoff_seconds=backoff_seconds,
                    max_backoff_seconds=max_backoff_seconds,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                retry_after = None

            try:
                result = await self._send_to_addresses(
                    client=client,
                    parsed=parsed,
                    hostname=hostname,
                    port=port,
                    addresses=addresses,
                    method=method,
                    headers=headers,
                    payload=payload,
                    timeout=timeout,
                    max_response_bytes=max_response_bytes,
                )
            except httpx.TransportError as exc:
                if attempt < max_attempts and self._can_retry_exception(method, exc):
                    continue
                return WebhookTransportResult(
                    success=False,
                    error=self._transport_error_message(exc),
                    attempts=attempt,
                )

            result.attempts = attempt
            if result.success or result.error_code is not None:
                return result
            if (
                attempt < max_attempts
                and method in IDEMPOTENT_METHODS
                and result.status_code in retry_statuses
            ):
                retry_after = (result.headers or {}).get("retry-after")
                continue
            return result

        raise RuntimeError("Webhook retry loop terminated without a result")

    async def _send_to_addresses(
        self,
        *,
        client: httpx.AsyncClient,
        parsed: SplitResult,
        hostname: str,
        port: int,
        addresses: tuple[str, ...],
        method: str,
        headers: dict[str, str],
        payload: dict[str, Any] | str | bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> WebhookTransportResult:
        last_connect_error: httpx.TransportError | None = None
        for address in addresses:
            try:
                return await self._send_request(
                    client=client,
                    parsed=parsed,
                    hostname=hostname,
                    port=port,
                    address=address,
                    method=method,
                    headers=headers,
                    payload=payload,
                    timeout=timeout,
                    max_response_bytes=max_response_bytes,
                )
            except _PROVABLY_UNSENT_ERRORS as exc:
                last_connect_error = exc

        if last_connect_error is not None:
            raise last_connect_error
        raise RuntimeError("Webhook address set was unexpectedly empty")

    async def _send_request(
        self,
        *,
        client: httpx.AsyncClient,
        parsed: SplitResult,
        hostname: str,
        port: int,
        address: str,
        method: str,
        headers: dict[str, str],
        payload: dict[str, Any] | str | bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> WebhookTransportResult:
        pinned_url = self._pinned_url(parsed, address, port)
        request_headers = dict(headers)
        request_headers["Host"] = self._host_header(parsed, hostname, port)
        request_kwargs: dict[str, Any] = {
            "headers": request_headers,
            "timeout": timeout,
        }
        if isinstance(payload, dict):
            request_kwargs["json"] = payload
        elif payload is not None:
            request_kwargs["content"] = payload

        request = client.build_request(method, pinned_url, **request_kwargs)
        if parsed.scheme == "https":
            request.extensions["sni_hostname"] = hostname.rstrip(".").encode("idna").decode()

        response = await client.send(request, stream=True, follow_redirects=False)
        response_headers = dict(response.headers)
        try:
            try:
                raw_body = await self._read_response_body(response, max_response_bytes)
            except _ResponseTooLargeError as exc:
                return WebhookTransportResult(
                    success=False,
                    status_code=response.status_code,
                    headers=response_headers,
                    error=f"Response body exceeds {max_response_bytes} bytes",
                    error_code="response_too_large",
                    diagnostics={
                        "captured_bytes": exc.captured_bytes,
                        "total_bytes": exc.total_bytes,
                        "url": sanitize_url(parsed.geturl()),
                    },
                )

            encoding = response.encoding or "utf-8"
            body = raw_body.decode(encoding, errors="replace")
            success = 200 <= response.status_code < 300
            return WebhookTransportResult(
                success=success,
                status_code=response.status_code,
                body=body,
                headers=response_headers,
                error=None if success else f"HTTP {response.status_code}",
            )
        finally:
            await response.aclose()

    @staticmethod
    async def _read_response_body(response: httpx.Response, limit: int) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > limit:
                raise _ResponseTooLargeError(
                    captured_bytes=limit + 1,
                    total_bytes=WebhookTransport._identity_content_length(response),
                )
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _identity_content_length(response: httpx.Response) -> int | None:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"", "identity"}:
            return None
        value = response.headers.get("content-length")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    @staticmethod
    def _retry_delay(
        *,
        retry_after: str | None,
        attempt: int,
        backoff_seconds: float,
        max_backoff_seconds: float,
    ) -> float:
        parsed_retry_after = parse_retry_after(retry_after, max_delay=max_backoff_seconds)
        if parsed_retry_after is not None:
            return parsed_retry_after
        return min(backoff_seconds * (2.0 ** (attempt - 2)), max_backoff_seconds)

    @staticmethod
    def _can_retry_exception(method: str, exc: httpx.TransportError) -> bool:
        return method in IDEMPOTENT_METHODS or isinstance(exc, _PROVABLY_UNSENT_ERRORS)

    @staticmethod
    def _transport_error_message(exc: httpx.TransportError) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "Request timeout"
        if isinstance(exc, httpx.ConnectError):
            return f"Connection error: {exc}"
        return str(exc) or type(exc).__name__

    @staticmethod
    def _validate_method(method: str) -> str:
        if not isinstance(method, str) or method.upper() not in ALLOWED_METHODS:
            supported = ", ".join(sorted(ALLOWED_METHODS))
            raise ValueError(f"Unsupported webhook method {method!r}; expected one of {supported}")
        return method.upper()

    @staticmethod
    def _validate_headers(headers: dict[str, str]) -> None:
        for name, value in headers.items():
            if not isinstance(name, str) or HEADER_NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(f"Invalid webhook header name: {name!r}")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise ValueError(f"Invalid webhook header value for {name!r}")

    @staticmethod
    def _validate_limits(
        *,
        timeout: float,
        max_response_bytes: int,
        max_attempts: int,
        backoff_seconds: float,
        max_backoff_seconds: float,
    ) -> None:
        if timeout <= 0:
            raise ValueError("Webhook timeout must be positive")
        if isinstance(max_response_bytes, bool) or max_response_bytes < 1:
            raise ValueError("Webhook response limit must be a positive integer")
        if isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("Webhook max attempts must be a positive integer")
        if backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("Webhook retry delays must be non-negative")

    @staticmethod
    def _parse_url(url: str) -> tuple[SplitResult, str, int]:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("Webhook URL is invalid") from exc
        if parsed.scheme not in {"http", "https"} or hostname is None:
            raise ValueError("Webhook URL must use http or https and include a hostname")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError("Webhook URL has an invalid port") from exc
        return parsed, hostname, port

    async def _lookup_addresses(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(hostname.split("%", 1)[0])
        except ValueError:
            try:
                records = await asyncio.get_running_loop().getaddrinfo(
                    hostname,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
            except socket.gaierror as exc:
                raise ValueError(f"Webhook hostname did not resolve: {hostname}") from exc
            addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records))
        else:
            addresses = (literal.compressed,)

        if not addresses:
            raise ValueError(f"Webhook hostname did not resolve: {hostname}")
        return addresses

    def _validate_addresses(self, addresses: tuple[str, ...]) -> None:
        for address in addresses:
            try:
                resolved_ip = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError as exc:
                raise ValueError(
                    f"Webhook hostname resolved to an invalid address: {address}"
                ) from exc
            if not self.allow_private_addresses and not resolved_ip.is_global:
                raise ValueError(
                    f"Webhook hostname resolves to a non-public address: {resolved_ip.compressed}"
                )

    @staticmethod
    def _pinned_url(parsed: SplitResult, address: str, port: int) -> str:
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        rendered_address = f"[{address}]" if ":" in address else address
        explicit_port = parsed.port is not None
        netloc = (
            f"{userinfo}{rendered_address}:{port}"
            if explicit_port
            else f"{userinfo}{rendered_address}"
        )
        return parsed._replace(netloc=netloc).geturl()

    @staticmethod
    def _host_header(parsed: SplitResult, hostname: str, port: int) -> str:
        rendered_hostname = f"[{hostname}]" if ":" in hostname else hostname
        return f"{rendered_hostname}:{port}" if parsed.port is not None else rendered_hostname
