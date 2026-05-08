"""Tests for the Linear GraphQL fallback client."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from gobby.integrations import linear_graphql
from gobby.integrations.linear_graphql import (
    LinearGraphQLClient,
    LinearGraphQLError,
    _parse_retry_after,
)

pytestmark = pytest.mark.unit


def _response(
    status_code: int,
    body: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=body or {"data": {"ok": True}},
        headers=headers,
        request=httpx.Request("POST", "https://api.linear.app/graphql"),
    )


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[Any]:
    calls: list[Any] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def post(
            self,
            endpoint: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            calls.append({"endpoint": endpoint, "headers": headers, "json": json})
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr("gobby.integrations.linear_graphql.httpx.AsyncClient", FakeAsyncClient)
    return calls


def _capture_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("gobby.integrations.linear_graphql.asyncio.sleep", fake_sleep)
    return delays


@pytest.mark.asyncio
async def test_execute_retries_rate_limit_with_retry_after_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.integrations.linear_graphql.random.uniform", lambda _low, _high: 0.0)
    calls = _install_fake_client(
        monkeypatch,
        [
            _response(429, headers={"Retry-After": "1.25"}),
            _response(200, {"data": {"viewer": {"id": "user-1"}}}),
        ],
    )
    delays = _capture_sleep(monkeypatch)

    result = await LinearGraphQLClient("lin-api-key").execute("query Viewer { viewer { id } }")

    assert result == {"viewer": {"id": "user-1"}}
    assert len(calls) == 2
    assert delays == [1.25]


def test_parse_numeric_retry_after_adds_bounded_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.integrations.linear_graphql.random.uniform", lambda _low, high: high)

    assert _parse_retry_after("1.0") == pytest.approx(1.1)
    assert _parse_retry_after("999") == 5.0


def test_parse_date_retry_after_adds_bounded_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(linear_graphql, "datetime", FrozenDateTime)
    monkeypatch.setattr("gobby.integrations.linear_graphql.random.uniform", lambda _low, high: high)
    retry_at = format_datetime(datetime(2026, 5, 8, 12, 0, 1, tzinfo=UTC))

    assert _parse_retry_after(retry_at) == pytest.approx(1.1)


@pytest.mark.asyncio
async def test_execute_wraps_retryable_status_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_client(
        monkeypatch,
        [
            _response(503),
            _response(503),
            _response(503),
        ],
    )
    delays = _capture_sleep(monkeypatch)

    with pytest.raises(LinearGraphQLError, match="HTTP 503"):
        await LinearGraphQLClient("lin-api-key").execute("query Teams { teams { nodes { id } } }")

    assert len(calls) == 3
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_execute_wraps_transport_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_client(
        monkeypatch,
        [
            httpx.TransportError("temporary network failure"),
            httpx.TransportError("temporary network failure"),
            httpx.TransportError("temporary network failure"),
        ],
    )
    delays = _capture_sleep(monkeypatch)

    with pytest.raises(LinearGraphQLError, match="network error"):
        await LinearGraphQLClient("lin-api-key").execute("query Teams { teams { nodes { id } } }")

    assert len(calls) == 3
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_execute_does_not_retry_non_retryable_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_client(monkeypatch, [_response(400)])
    delays = _capture_sleep(monkeypatch)

    with pytest.raises(LinearGraphQLError, match="HTTP 400"):
        await LinearGraphQLClient("lin-api-key").execute("query Broken { nope }")

    assert len(calls) == 1
    assert delays == []


@pytest.mark.asyncio
async def test_execute_wraps_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_client(
        monkeypatch,
        [
            httpx.Response(
                200,
                content=b"not-json",
                request=httpx.Request("POST", "https://api.linear.app/graphql"),
            ),
        ],
    )

    with pytest.raises(LinearGraphQLError, match="not valid JSON"):
        await LinearGraphQLClient("lin-api-key").execute("query Broken { nope }")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_execute_preserves_graphql_error_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_client(
        monkeypatch,
        [_response(200, {"errors": [{"message": "Invalid query"}]})],
    )

    with pytest.raises(LinearGraphQLError, match="Invalid query"):
        await LinearGraphQLClient("lin-api-key").execute("query Broken { nope }")

    assert len(calls) == 1
