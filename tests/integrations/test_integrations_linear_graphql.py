from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.integrations import linear_graphql
from gobby.integrations.linear_graphql import LinearGraphQLClient, LinearGraphQLError


def _connection(
    nodes: list[dict[str, object]],
    *,
    has_next_page: bool,
    end_cursor: str | None,
) -> dict[str, object]:
    return {
        "nodes": nodes,
        "pageInfo": {
            "hasNextPage": has_next_page,
            "endCursor": end_cursor,
        },
    }


def _nested_connection(path: tuple[str, ...], connection: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = connection
    for key in reversed(path):
        result = {key: result}
    return result


def _response(
    status_code: int,
    body: dict[str, object] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=body if body is not None else {"data": {"ok": True}},
        headers=headers or {},
        request=httpx.Request("POST", "https://api.linear.app/graphql"),
    )


def _mock_http_client(*side_effects: object) -> AsyncMock:
    async_client = AsyncMock()
    async_client.post.side_effect = side_effects
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__.return_value = False
    return async_client


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_execute_retry_error_uses_attempt_constant() -> None:
    client = LinearGraphQLClient("lin_api_key")
    async_client = AsyncMock()
    async_client.post.side_effect = httpx.TimeoutException("timed out")
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__.return_value = False

    with (
        patch("gobby.integrations.linear_graphql.httpx.AsyncClient", return_value=async_client),
        patch("gobby.integrations.linear_graphql.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(LinearGraphQLError) as exc_info:
            await client.execute("query Test { viewer { id } }")

    assert f"{linear_graphql._MAX_ATTEMPTS} attempts" in str(exc_info.value)
    assert async_client.post.await_count == linear_graphql._MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_execute_retries_429_after_server_delay() -> None:
    rate_limited = httpx.Response(
        429,
        headers={"Retry-After": "3"},
        request=httpx.Request("POST", "https://api.linear.app/graphql"),
    )
    success = _response(200, {"data": {"viewer": {"id": "viewer-1"}}})
    async_client = _mock_http_client(rate_limited, success)
    client = LinearGraphQLClient("lin_api_key")
    sleep = AsyncMock()

    with (
        patch("gobby.integrations.linear_graphql.httpx.AsyncClient", return_value=async_client),
        patch("gobby.integrations.linear_graphql.asyncio.sleep", new=sleep),
        patch("gobby.integrations.linear_graphql.random.uniform", return_value=0.0),
    ):
        data = await client.execute("query Test { viewer { id } }")

    assert data == {"viewer": {"id": "viewer-1"}}
    sleep.assert_awaited_once_with(3.0)
    assert async_client.post.await_count == 2


@pytest.mark.asyncio
async def test_list_issues_paginates_past_one_hundred_and_stops_at_terminal_cursor() -> None:
    client = LinearGraphQLClient("lin_api_key")
    first_page = [{"id": f"issue-{index}"} for index in range(100)]
    second_page = [{"id": f"issue-{index}"} for index in range(100, 125)]
    client.execute = AsyncMock(
        side_effect=[
            {"issues": _connection(first_page, has_next_page=True, end_cursor="cursor-100")},
            {"issues": _connection(second_page, has_next_page=False, end_cursor=None)},
        ]
    )

    issues = await client.list_issues(team_id="team-1")

    assert [issue["id"] for issue in issues] == [f"issue-{index}" for index in range(125)]
    assert [call.args[1]["after"] for call in client.execute.await_args_list] == [
        None,
        "cursor-100",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "method_args", "connection_path"),
    [
        ("list_teams", (), ("teams",)),
        ("list_projects", ("team-1",), ("team", "projects")),
        ("list_team_states", ("team-1",), ("team", "states")),
    ],
)
async def test_linear_list_connections_paginate_all_pages(
    method_name: str,
    method_args: tuple[str, ...],
    connection_path: tuple[str, ...],
) -> None:
    client = LinearGraphQLClient("lin_api_key")
    client.execute = AsyncMock(
        side_effect=[
            _nested_connection(
                connection_path,
                _connection([{"id": "first"}], has_next_page=True, end_cursor="next"),
            ),
            _nested_connection(
                connection_path,
                _connection([{"id": "second"}], has_next_page=False, end_cursor=None),
            ),
        ]
    )

    method = getattr(client, method_name)
    records = await method(*method_args)

    assert [record["id"] for record in records] == ["first", "second"]
    assert client.execute.await_count == 2


@pytest.mark.asyncio
async def test_linear_pagination_rejects_missing_or_repeated_next_cursor() -> None:
    client = LinearGraphQLClient("lin_api_key")
    client.execute = AsyncMock(
        return_value={
            "issues": _connection([{"id": "partial"}], has_next_page=True, end_cursor=None)
        }
    )

    with pytest.raises(LinearGraphQLError, match="endCursor"):
        await client.list_issues(team_id="team-1")


@pytest.mark.asyncio
async def test_linear_pagination_rejects_missing_page_info() -> None:
    client = LinearGraphQLClient("lin_api_key")
    client.execute = AsyncMock(return_value={"issues": {"nodes": [{"id": "partial"}]}})

    with pytest.raises(LinearGraphQLError, match="pageInfo"):
        await client.list_issues(team_id="team-1")


@pytest.mark.asyncio
async def test_list_projects_chains_fallback_failure_to_primary_error() -> None:
    client = LinearGraphQLClient("lin_api_key")
    primary = LinearGraphQLError("team projects failed")
    fallback = LinearGraphQLError("global projects failed")
    client.execute = AsyncMock(side_effect=[primary, fallback])

    with pytest.raises(LinearGraphQLError, match="global projects failed") as exc_info:
        await client.list_projects("team-1")

    assert exc_info.value.__cause__ is primary


async def _create_issue(client: LinearGraphQLClient) -> dict[str, object]:
    return await client.create_issue(
        team_id="team-1",
        title="No duplicate",
        description="",
        priority=2,
        project_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_failure",
    [
        httpx.ReadTimeout(
            "response timed out",
            request=httpx.Request("POST", "https://api.linear.app/graphql"),
        ),
        httpx.WriteError(
            "request body may have been sent",
            request=httpx.Request("POST", "https://api.linear.app/graphql"),
        ),
        _response(503, {"errors": [{"message": "unavailable"}]}),
    ],
)
async def test_non_idempotent_creation_never_retries_ambiguous_failures(
    first_failure: object,
) -> None:
    success = _response(
        200,
        {"data": {"issueCreate": {"issue": {"id": "duplicate-if-retried"}}}},
    )
    async_client = _mock_http_client(first_failure, success)
    client = LinearGraphQLClient("lin_api_key")

    with (
        patch("gobby.integrations.linear_graphql.httpx.AsyncClient", return_value=async_client),
        patch("gobby.integrations.linear_graphql.asyncio.sleep", new=AsyncMock()),
        pytest.raises(LinearGraphQLError),
    ):
        await _create_issue(client)

    assert async_client.post.await_count == 1


@pytest.mark.asyncio
async def test_non_idempotent_creation_does_not_retry_429() -> None:
    rate_limited = httpx.Response(
        429,
        headers={"Retry-After": "12"},
        request=httpx.Request("POST", "https://api.linear.app/graphql"),
    )
    async_client = _mock_http_client(rate_limited)
    client = LinearGraphQLClient("lin_api_key")
    sleep = AsyncMock()

    with (
        patch("gobby.integrations.linear_graphql.httpx.AsyncClient", return_value=async_client),
        patch("gobby.integrations.linear_graphql.asyncio.sleep", new=sleep),
        pytest.raises(LinearGraphQLError, match="HTTP 429"),
    ):
        await _create_issue(client)

    sleep.assert_not_awaited()
    assert async_client.post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_non_idempotent_creation_retries_connect_establishment_failure(
    failure_type: Callable[..., httpx.RequestError],
) -> None:
    request = httpx.Request("POST", "https://api.linear.app/graphql")
    success = _response(200, {"data": {"issueCreate": {"issue": {"id": "created"}}}})
    async_client = _mock_http_client(failure_type("connect failed", request=request), success)
    client = LinearGraphQLClient("lin_api_key")

    with (
        patch("gobby.integrations.linear_graphql.httpx.AsyncClient", return_value=async_client),
        patch("gobby.integrations.linear_graphql.asyncio.sleep", new=AsyncMock()),
    ):
        issue = await _create_issue(client)

    assert issue["id"] == "created"
    assert async_client.post.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["create_issue", "create_project"])
async def test_creation_methods_mark_graphql_execution_non_idempotent(method_name: str) -> None:
    client = LinearGraphQLClient("lin_api_key")
    client.execute = AsyncMock(
        return_value={
            "issueCreate": {"issue": {"id": "issue-1"}},
            "projectCreate": {"project": {"id": "project-1"}},
        }
    )

    if method_name == "create_issue":
        record = await _create_issue(client)
        assert record["id"] == "issue-1"
    else:
        record = await client.create_project("team-1", "Project")
        assert record["id"] == "project-1"

    assert client.execute.await_args.kwargs["idempotent"] is False


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


async def test_from_database_async_reads_secret_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    class FakeSecretStore:
        def __init__(self, _db: object) -> None:
            pass

        def get(self, _name: str) -> str:
            worker_threads.append(threading.get_ident())
            return "lin_api_key"

    monkeypatch.setattr(linear_graphql, "SecretStore", FakeSecretStore)

    client = await LinearGraphQLClient.from_database_async(object())  # type: ignore[arg-type]

    assert isinstance(client, LinearGraphQLClient)
    assert len(worker_threads) == 1
    assert worker_threads[0] != loop_thread


def test_parse_numeric_retry_after_adds_bounded_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.integrations.linear_graphql.random.uniform", lambda _low, high: high)

    assert linear_graphql._retry_delay(0, _response(429, headers={"Retry-After": "1.0"})) == (
        pytest.approx(1.1)
    )
    assert linear_graphql._retry_delay(0, _response(429, headers={"Retry-After": "999"})) == 5.0


def test_parse_date_retry_after_adds_bounded_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr("gobby.utils.http_retry.datetime", FrozenDateTime)
    monkeypatch.setattr("gobby.integrations.linear_graphql.random.uniform", lambda _low, high: high)
    retry_at = format_datetime(datetime(2026, 5, 8, 12, 0, 1, tzinfo=UTC))

    response = _response(429, headers={"Retry-After": retry_at})
    assert linear_graphql._retry_delay(0, response) == pytest.approx(1.1)


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
