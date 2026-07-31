"""Minimal Linear GraphQL client for operations not exposed by the MCP server."""

from __future__ import annotations

import asyncio
import random
from typing import Any, cast

import httpx

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.utils.http_retry import parse_retry_after

__all__ = ["LinearGraphQLClient", "LinearGraphQLError"]


_MAX_ATTEMPTS = 3
_INITIAL_RETRY_DELAY_SECONDS = 0.5
_MAX_RETRY_DELAY_SECONDS = 5.0
_RETRY_AFTER_JITTER_FRACTION = 0.1
_MAX_RETRY_AFTER_JITTER_SECONDS = 0.5


class LinearGraphQLError(RuntimeError):
    """Raised when the Linear GraphQL API returns an error."""


class LinearGraphQLClient:
    """Small async client for Linear's public GraphQL API."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.linear.app/graphql",
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout

    @classmethod
    def from_database(cls, db: HubDatabase) -> LinearGraphQLClient | None:
        """Build a client from the stored Linear API key, if configured."""
        api_key = SecretStore(db).get("linear_api_key")
        if not api_key:
            return None
        return cls(api_key)

    @classmethod
    async def from_database_async(cls, db: HubDatabase) -> LinearGraphQLClient | None:
        """Build a client from the stored Linear API key without blocking the event loop."""
        return await asyncio.to_thread(cls.from_database, db)

    async def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {"query": query, "variables": variables or {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response: httpx.Response | None = None
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    response = await client.post(self.endpoint, headers=headers, json=payload)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    can_retry = idempotent or _is_connection_establishment_failure(exc)
                    if not can_retry or _is_final_attempt(attempt):
                        attempts = attempt + 1
                        raise LinearGraphQLError(
                            "Linear GraphQL request failed after "
                            f"{attempts} {_attempt_word(attempts)} due to a network error."
                        ) from exc
                    await asyncio.sleep(_retry_delay(attempt))
                    continue

                if _is_retryable_status(response.status_code):
                    if not idempotent or _is_final_attempt(attempt):
                        attempts = attempt + 1
                        raise LinearGraphQLError(
                            "Linear GraphQL request failed after "
                            f"{attempts} {_attempt_word(attempts)} "
                            f"with HTTP {response.status_code}."
                        ) from _status_error(response)
                    await asyncio.sleep(_retry_delay(attempt, response))
                    continue

                break

            if response is None:
                raise LinearGraphQLError("Linear GraphQL request did not produce a response.")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise LinearGraphQLError(
                    f"Linear GraphQL request failed with HTTP {response.status_code}."
                ) from exc
            try:
                body = response.json()
            except ValueError as exc:
                raise LinearGraphQLError("Linear GraphQL response was not valid JSON.") from exc
            if not isinstance(body, dict):
                raise LinearGraphQLError("Linear GraphQL response was not a JSON object.")
            errors = body.get("errors")
            if errors:
                messages = [
                    str(error.get("message", error)) if isinstance(error, dict) else str(error)
                    for error in errors
                ]
                raise LinearGraphQLError("; ".join(messages))
            data = body.get("data")
            if not isinstance(data, dict):
                raise LinearGraphQLError("Linear GraphQL response did not include data.")
            return cast(dict[str, Any], data)

    async def _paginate_connection(
        self,
        query: str,
        variables: dict[str, Any],
        connection_path: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        after: str | None = None
        seen_cursors: set[str] = set()

        while True:
            data = await self.execute(query, {**variables, "after": after})
            connection = _connection_at_path(data, connection_path)
            records.extend(_connection_nodes(connection))
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(
                page_info.get("hasNextPage"), bool
            ):
                raise LinearGraphQLError(
                    "Linear GraphQL connection did not include valid pageInfo."
                )
            if page_info["hasNextPage"] is False:
                return records

            end_cursor = page_info.get("endCursor")
            if not isinstance(end_cursor, str) or not end_cursor or end_cursor in seen_cursors:
                raise LinearGraphQLError(
                    "Linear GraphQL pagination reported another page without a new endCursor."
                )
            seen_cursors.add(end_cursor)
            after = end_cursor

    async def list_teams(self) -> list[dict[str, Any]]:
        return await self._paginate_connection(
            """
            query Teams($after: String) {
              teams(first: 100, after: $after) {
                nodes {
                  id
                  name
                  key
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
            """,
            {},
            ("teams",),
        )

    async def list_projects(self, team_id: str) -> list[dict[str, Any]]:
        try:
            return await self._paginate_connection(
                """
                query TeamProjects($teamId: String!, $after: String) {
                  team(id: $teamId) {
                    projects(first: 100, after: $after) {
                      nodes {
                        id
                        name
                      }
                      pageInfo {
                        hasNextPage
                        endCursor
                      }
                    }
                  }
                }
                """,
                {"teamId": team_id},
                ("team", "projects"),
            )
        except LinearGraphQLError as primary_error:
            try:
                projects = await self._paginate_connection(
                    """
                    query Projects($after: String) {
                      projects(first: 100, after: $after) {
                        nodes {
                          id
                          name
                          teams {
                            nodes {
                              id
                            }
                          }
                        }
                        pageInfo {
                          hasNextPage
                          endCursor
                        }
                      }
                    }
                    """,
                    {},
                    ("projects",),
                )
            except LinearGraphQLError as fallback_error:
                raise fallback_error from primary_error
            return [
                project
                for project in projects
                if any(
                    team.get("id") == team_id for team in _connection_nodes(project.get("teams"))
                )
            ]

    async def list_team_states(self, team_id: str) -> list[dict[str, Any]]:
        return await self._paginate_connection(
            """
            query TeamStates($teamId: String!, $after: String) {
              team(id: $teamId) {
                states(first: 100, after: $after) {
                  nodes {
                    id
                    name
                    type
                  }
                  pageInfo {
                    hasNextPage
                    endCursor
                  }
                }
              }
            }
            """,
            {"teamId": team_id},
            ("team", "states"),
        )

    async def create_project(self, team_id: str, name: str) -> dict[str, Any]:
        data = await self.execute(
            """
            mutation ProjectCreate($input: ProjectCreateInput!) {
              projectCreate(input: $input) {
                success
                project {
                  id
                  name
                }
              }
            }
            """,
            {"input": {"name": name, "teamIds": [team_id]}},
            idempotent=False,
        )
        payload = _payload(data, "projectCreate")
        project = payload.get("project")
        if not isinstance(project, dict) or not project.get("id"):
            raise LinearGraphQLError("Linear projectCreate did not return a project id.")
        return cast(dict[str, Any], project)

    async def list_issues(
        self,
        *,
        team_id: str,
        project_id: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        issue_filter: dict[str, Any] = {"team": {"id": {"eq": team_id}}}
        if project_id:
            issue_filter["project"] = {"id": {"eq": project_id}}
        if state:
            issue_filter["state"] = {"name": {"eq": state}}
        if labels:
            issue_filter["labels"] = {"name": {"in": labels}}

        return await self._paginate_connection(
            """
            query Issues($filter: IssueFilter, $after: String) {
              issues(first: 100, after: $after, filter: $filter) {
                nodes {
                  id
                  identifier
                  title
                  description
                  priority
                  updatedAt
                  archivedAt
                  state {
                    name
                  }
                  project {
                    id
                    name
                  }
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
            """,
            {"filter": issue_filter},
            ("issues",),
        )

    async def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        priority: int,
        project_id: str | None,
    ) -> dict[str, Any]:
        issue_input: dict[str, Any] = {
            "teamId": team_id,
            "title": title,
            "description": description,
            "priority": priority,
        }
        if project_id:
            issue_input["projectId"] = project_id

        data = await self.execute(
            """
            mutation IssueCreate($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue {
                  id
                  identifier
                  url
                  title
                }
              }
            }
            """,
            {"input": issue_input},
            idempotent=False,
        )
        payload = _payload(data, "issueCreate")
        issue = payload.get("issue")
        if not isinstance(issue, dict) or not issue.get("id"):
            raise LinearGraphQLError("Linear issueCreate did not return an issue id.")
        return cast(dict[str, Any], issue)

    async def update_issue(
        self,
        *,
        issue_id: str,
        title: str,
        description: str,
        priority: int,
        state_id: str | None = None,
    ) -> dict[str, Any]:
        update_input: dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": priority,
        }
        if state_id:
            update_input["stateId"] = state_id

        data = await self.execute(
            """
            mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) {
                success
                issue {
                  id
                  identifier
                  url
                  title
                }
              }
            }
            """,
            {"id": issue_id, "input": update_input},
        )
        payload = _payload(data, "issueUpdate")
        issue = payload.get("issue")
        if not isinstance(issue, dict) or not issue.get("id"):
            raise LinearGraphQLError("Linear issueUpdate did not return an issue id.")
        return cast(dict[str, Any], issue)

    async def delete_issue(self, issue_id: str) -> bool:
        data = await self.execute(
            """
            mutation IssueDelete($id: String!) {
              issueDelete(id: $id) {
                success
              }
            }
            """,
            {"id": issue_id},
        )
        payload = _payload(data, "issueDelete")
        return bool(payload.get("success"))


def _payload(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise LinearGraphQLError(f"Linear response did not include {key}.")
    return cast(dict[str, Any], value)


def _connection_nodes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _connection_at_path(data: dict[str, Any], connection_path: tuple[str, ...]) -> dict[str, Any]:
    value: Any = data
    for key in connection_path:
        if not isinstance(value, dict) or key not in value:
            joined_path = ".".join(connection_path)
            raise LinearGraphQLError(
                f"Linear GraphQL response did not include connection {joined_path}."
            )
        value = value[key]
    if not isinstance(value, dict):
        joined_path = ".".join(connection_path)
        raise LinearGraphQLError(f"Linear GraphQL connection {joined_path} was not an object.")
    return cast(dict[str, Any], value)


def _is_connection_establishment_failure(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def _attempt_word(attempts: int) -> str:
    return "attempt" if attempts == 1 else "attempts"


def _is_final_attempt(attempt: int) -> bool:
    return attempt >= _MAX_ATTEMPTS - 1


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    if response is not None:
        parsed_retry_after = parse_retry_after(
            response.headers.get("Retry-After"),
            max_delay=_MAX_RETRY_DELAY_SECONDS,
        )
        if parsed_retry_after is not None:
            return _bounded_retry_after_delay(parsed_retry_after)
    return float(min(_INITIAL_RETRY_DELAY_SECONDS * (2**attempt), _MAX_RETRY_DELAY_SECONDS))


def _bounded_retry_after_delay(delay: float) -> float:
    # random.uniform here is retry-backoff jitter, never used for security tokens.
    capped_delay = min(delay, _MAX_RETRY_DELAY_SECONDS)
    jitter_max = min(capped_delay * _RETRY_AFTER_JITTER_FRACTION, _MAX_RETRY_AFTER_JITTER_SECONDS)
    return min(
        _MAX_RETRY_DELAY_SECONDS,
        capped_delay + random.uniform(0.0, jitter_max),  # nosec B311
    )


def _status_error(response: httpx.Response) -> httpx.HTTPStatusError:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    return httpx.HTTPStatusError(
        f"Unexpected retryable HTTP status {response.status_code}",
        request=response.request,
        response=response,
    )
