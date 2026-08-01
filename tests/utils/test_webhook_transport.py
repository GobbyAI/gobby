"""Tests for the shared outbound webhook transport."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.utils.webhook_transport import DEFAULT_MAX_RESPONSE_BYTES, WebhookTransport

pytestmark = pytest.mark.unit


async def test_private_literal_is_rejected_before_client_creation() -> None:
    transport = WebhookTransport()

    with patch("gobby.utils.webhook_transport.httpx.AsyncClient") as client:
        with pytest.raises(ValueError, match="non-public address"):
            await transport.execute("http://127.0.0.1/hook")

    client.assert_not_called()


async def test_every_resolved_address_is_validated_before_client_creation() -> None:
    transport = WebhookTransport()

    with (
        patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34", "127.0.0.1")),
        ),
        patch("gobby.utils.webhook_transport.httpx.AsyncClient") as client,
    ):
        with pytest.raises(ValueError, match="127.0.0.1"):
            await transport.execute("https://hooks.example/callback")

    client.assert_not_called()


async def test_private_addresses_can_be_allowed_and_remain_pinned() -> None:
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    transport = WebhookTransport(allow_private_addresses=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("127.0.0.1",)),
        ):
            result = await transport.execute(
                "http://internal.example/hook",
                client=client,
            )

    assert result.success is True
    assert requests[0].url.host == "127.0.0.1"
    assert requests[0].headers["host"] == "internal.example"


async def test_pinned_https_request_preserves_original_tls_hostname() -> None:
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"ok")

    transport = WebhookTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ):
            result = await transport.execute(
                "https://hooks.example:8443/callback?token=secret",
                client=client,
            )

    assert result.success is True
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "hooks.example:8443"
    # httpcore uses this extension as TLS server_hostname for SNI and cert verification.
    assert requests[0].extensions["sni_hostname"] == "hooks.example"


async def test_multiple_pinned_addresses_fail_over_on_connect_error() -> None:
    hosts: list[str] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.host is not None
        hosts.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("first address offline", request=request)
        return httpx.Response(200, content=b"ok")

    transport = WebhookTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34", "93.184.216.35")),
        ):
            result = await transport.execute("https://hooks.example/hook", client=client)

    assert result.success is True
    assert result.attempts == 1
    assert hosts == ["93.184.216.34", "93.184.216.35"]


class ChunkedStream(httpx.AsyncByteStream):
    """Observable response stream that exposes mid-stream overflow behavior."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_response_cap_stops_chunked_body_mid_stream() -> None:
    stream = ChunkedStream([b"1234", b"5678", b"9", b"unread"])

    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    transport = WebhookTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ):
            result = await transport.execute(
                "https://user:pass@hooks.example/hook?token=secret#fragment",
                client=client,
                max_response_bytes=8,
            )

    assert result.success is False
    assert result.error_code == "response_too_large"
    assert result.error == "Response body exceeds 8 bytes"
    assert result.diagnostics == {
        "captured_bytes": 9,
        "total_bytes": None,
        "url": "https://hooks.example/hook",
    }
    assert stream.yielded == 3
    assert stream.closed is True


async def test_declared_response_size_is_reported_for_identity_encoding() -> None:
    stream = ChunkedStream([b"123456789"])

    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "12"}, stream=stream)

    transport = WebhookTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ):
            result = await transport.execute(
                "https://hooks.example/hook",
                client=client,
                max_response_bytes=8,
            )

    assert result.diagnostics is not None
    assert result.diagnostics["total_bytes"] == 12


async def test_internal_client_disables_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    real_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    transport = WebhookTransport()

    with (
        patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ),
        patch(
            "gobby.utils.webhook_transport.httpx.AsyncClient",
            return_value=real_client,
        ) as client_class,
    ):
        result = await transport.execute("https://hooks.example/hook")

    assert result.success is True
    assert client_class.call_args.kwargs["trust_env"] is False
    assert client_class.call_args.kwargs["follow_redirects"] is False


async def test_idempotent_status_failure_is_retried() -> None:
    responses = iter([httpx.Response(503), httpx.Response(200, content=b"ok")])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: next(responses)))
    transport = WebhookTransport()

    async with client:
        with (
            patch.object(
                transport,
                "_lookup_addresses",
                new=AsyncMock(return_value=("93.184.216.34",)),
            ),
            patch("gobby.utils.webhook_transport.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            result = await transport.execute(
                "https://hooks.example/hook",
                method="GET",
                client=client,
                max_attempts=2,
                backoff_seconds=0.25,
            )

    assert result.success is True
    assert result.attempts == 2
    sleep.assert_awaited_once_with(0.25)


async def test_non_idempotent_status_failure_is_not_retried() -> None:
    calls = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    transport = WebhookTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ):
            result = await transport.execute(
                "https://hooks.example/hook",
                method="POST",
                client=client,
                max_attempts=3,
                backoff_seconds=0,
            )

    assert result.success is False
    assert result.attempts == 1
    assert calls == 1


async def test_non_idempotent_connect_failure_is_retried() -> None:
    calls = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200)

    transport = WebhookTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with (
            patch.object(
                transport,
                "_lookup_addresses",
                new=AsyncMock(return_value=("93.184.216.34",)),
            ),
            patch("gobby.utils.webhook_transport.asyncio.sleep", new=AsyncMock()),
        ):
            result = await transport.execute(
                "https://hooks.example/hook",
                method="POST",
                client=client,
                max_attempts=2,
                backoff_seconds=0,
            )

    assert result.success is True
    assert result.attempts == 2
    assert calls == 2


async def test_non_idempotent_read_timeout_is_not_retried() -> None:
    calls = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out after send", request=request)

    transport = WebhookTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ):
            result = await transport.execute(
                "https://hooks.example/hook",
                method="POST",
                client=client,
                max_attempts=3,
                backoff_seconds=0,
            )

    assert result.success is False
    assert result.attempts == 1
    assert calls == 1


async def test_retry_after_and_exponential_backoff_are_capped() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "999"}),
            httpx.Response(503),
            httpx.Response(200),
        ]
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: next(responses)))
    transport = WebhookTransport()

    async with client:
        with (
            patch.object(
                transport,
                "_lookup_addresses",
                new=AsyncMock(return_value=("93.184.216.34",)),
            ),
            patch("gobby.utils.webhook_transport.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            result = await transport.execute(
                "https://hooks.example/hook",
                method="GET",
                client=client,
                max_attempts=3,
                backoff_seconds=40,
                max_backoff_seconds=60,
            )

    assert result.success is True
    assert [call.args[0] for call in sleep.await_args_list] == [60, 60]


@pytest.mark.parametrize("method", ["CONNECT", "OPTIONS", "TRACE", ""])
async def test_method_allowlist_is_enforced(method: str) -> None:
    with pytest.raises(ValueError, match="Unsupported webhook method"):
        await WebhookTransport().execute("https://hooks.example/hook", method=method)


@pytest.mark.parametrize(
    "headers",
    [
        {"Bad Header": "value"},
        {"X-Test": "line1\r\nInjected: true"},
    ],
)
async def test_hostile_headers_are_rejected(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="Invalid webhook header"):
        await WebhookTransport().execute("https://hooks.example/hook", headers=headers)


async def test_default_response_limit_accepts_exact_boundary() -> None:
    body = b"x" * DEFAULT_MAX_RESPONSE_BYTES
    transport = WebhookTransport()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body))
    ) as client:
        with patch.object(
            transport,
            "_lookup_addresses",
            new=AsyncMock(return_value=("93.184.216.34",)),
        ):
            result = await transport.execute("https://hooks.example/hook", client=client)

    assert result.success is True
    assert result.body == body.decode()
