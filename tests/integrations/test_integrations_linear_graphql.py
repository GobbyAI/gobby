from __future__ import annotations

from collections.abc import Callable
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


def _response(status_code: int, body: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=body,
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
        await _create_issue(client)
    else:
        await client.create_project("team-1", "Project")

    assert client.execute.await_args.kwargs["idempotent"] is False
