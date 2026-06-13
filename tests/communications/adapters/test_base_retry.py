"""Tests for BaseChannelAdapter._retry_request."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.base import _MAX_RETRY_AFTER_DELAY_SECONDS, BaseChannelAdapter
from gobby.communications.adapters.discord import DiscordAdapter
from gobby.communications.adapters.email import EmailAdapter
from gobby.communications.adapters.gobby_chat import GobbyChatAdapter
from gobby.communications.adapters.slack import SlackAdapter
from gobby.communications.adapters.sms import SMSAdapter
from gobby.communications.adapters.teams import TeamsAdapter
from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import ChannelCapabilities, CommsMessage

pytestmark = pytest.mark.unit


class ConcreteAdapter(BaseChannelAdapter):
    """Minimal concrete adapter for testing base class methods."""

    @property
    def channel_type(self) -> str:
        return "test"

    @property
    def max_message_length(self) -> int:
        return 1000

    @property
    def supports_webhooks(self) -> bool:
        return False

    @property
    def supports_polling(self) -> bool:
        return False

    async def initialize(self, config, secret_resolver):
        pass

    async def send_message(self, message: CommsMessage) -> str | None:
        return None

    async def shutdown(self):
        pass

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            threading=False, reactions=False, files=False, markdown=False, max_message_length=1000
        )

    def parse_webhook(self, payload, headers):
        return []

    def verify_webhook(self, payload, headers, secret):
        return False


def _make_response(status_code: int, headers: dict | None = None) -> MagicMock:
    """Create a mock httpx.Response with the given status code."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture
def adapter() -> ConcreteAdapter:
    return ConcreteAdapter()


@pytest.mark.parametrize(
    "adapter_type",
    [
        DiscordAdapter,
        EmailAdapter,
        GobbyChatAdapter,
        SlackAdapter,
        SMSAdapter,
        TeamsAdapter,
        TelegramAdapter,
    ],
)
def test_channel_adapters_initialize_base_rate_limit_state(
    adapter_type: type[BaseChannelAdapter],
) -> None:
    adapter = adapter_type()

    assert adapter._rate_limit_callback is None


@pytest.mark.asyncio
async def test_success_returns_immediately(adapter: ConcreteAdapter) -> None:
    response = _make_response(200)
    factory = AsyncMock(return_value=response)

    result = await adapter._retry_request(factory, max_retries=3)

    assert result is response
    assert factory.await_count == 1


@pytest.mark.asyncio
@patch("gobby.communications.adapters.base.asyncio.sleep", new_callable=AsyncMock)
async def test_429_with_retry_after_header(mock_sleep: AsyncMock, adapter: ConcreteAdapter) -> None:
    rate_limited = _make_response(429, headers={"Retry-After": "2.5"})
    success = _make_response(200)
    factory = AsyncMock(side_effect=[rate_limited, success])

    result = await adapter._retry_request(factory, max_retries=3)

    assert result is success
    assert factory.await_count == 2
    mock_sleep.assert_awaited_once_with(2.5)


@pytest.mark.asyncio
@patch("gobby.communications.adapters.base.asyncio.sleep", new_callable=AsyncMock)
async def test_429_with_oversized_retry_after_clamps_sleep_and_rate_limit_callback(
    mock_sleep: AsyncMock, adapter: ConcreteAdapter
) -> None:
    rate_limit_callback = MagicMock()
    adapter.set_rate_limit_callback(rate_limit_callback)
    rate_limited = _make_response(429, headers={"Retry-After": "999999"})
    success = _make_response(200)
    factory = AsyncMock(side_effect=[rate_limited, success])

    result = await adapter._retry_request(factory, max_retries=3)

    assert result is success
    rate_limit_callback.assert_called_once_with(_MAX_RETRY_AFTER_DELAY_SECONDS, False)
    mock_sleep.assert_awaited_once_with(_MAX_RETRY_AFTER_DELAY_SECONDS)


@pytest.mark.asyncio
@patch("gobby.communications.adapters.base.asyncio.sleep", new_callable=AsyncMock)
async def test_429_without_retry_after_uses_backoff(
    mock_sleep: AsyncMock, adapter: ConcreteAdapter
) -> None:
    rate_limited = _make_response(429)
    success = _make_response(200)
    factory = AsyncMock(side_effect=[rate_limited, success])

    result = await adapter._retry_request(factory, max_retries=3, backoff_base=1.0)

    assert result is success
    # First attempt (attempt=0): backoff = 1.0 * 2^0 = 1.0
    mock_sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
@patch("gobby.communications.adapters.base.asyncio.sleep", new_callable=AsyncMock)
async def test_5xx_retries_with_backoff(mock_sleep: AsyncMock, adapter: ConcreteAdapter) -> None:
    error_500 = _make_response(500)
    error_502 = _make_response(502)
    success = _make_response(200)
    factory = AsyncMock(side_effect=[error_500, error_502, success])

    result = await adapter._retry_request(factory, max_retries=3, backoff_base=1.0)

    assert result is success
    assert factory.await_count == 3
    # attempt 0: 1.0 * 2^0 = 1.0, attempt 1: 1.0 * 2^1 = 2.0
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(1.0)
    mock_sleep.assert_any_await(2.0)


@pytest.mark.asyncio
async def test_4xx_non_429_raises_immediately(adapter: ConcreteAdapter) -> None:
    error_403 = _make_response(403)
    factory = AsyncMock(return_value=error_403)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter._retry_request(factory, max_retries=3)

    assert factory.await_count == 1


@pytest.mark.asyncio
@patch("gobby.communications.adapters.base.asyncio.sleep", new_callable=AsyncMock)
async def test_exhausted_retries_raises(mock_sleep: AsyncMock, adapter: ConcreteAdapter) -> None:
    error_500 = _make_response(500)
    factory = AsyncMock(return_value=error_500)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter._retry_request(factory, max_retries=2)

    # 3 total attempts (initial + 2 retries), last one raises
    assert factory.await_count == 3


@pytest.mark.asyncio
@patch("gobby.communications.adapters.base.asyncio.sleep", new_callable=AsyncMock)
async def test_429_exhausted_retries_raises(
    mock_sleep: AsyncMock, adapter: ConcreteAdapter
) -> None:
    rate_limited = _make_response(429)
    factory = AsyncMock(return_value=rate_limited)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter._retry_request(factory, max_retries=1)

    # 429 retries all attempts including the last, then raises
    assert factory.await_count == 2
