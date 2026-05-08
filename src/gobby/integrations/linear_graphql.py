"""Minimal Linear GraphQL client for operations not exposed by the MCP server."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx

from gobby.storage.database import DatabaseProtocol
from gobby.storage.secrets import SecretStore

__all__ = ["LinearGraphQLClient", "LinearGraphQLError"]


_MAX_ATTEMPTS = 3
_INITIAL_RETRY_DELAY_SECONDS = 0.5
_MAX_RETRY_DELAY_SECONDS = 5.0


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
    def from_database(cls, db: DatabaseProtocol) -> LinearGraphQLClient | None:
        """Build a client from the stored Linear API key, if configured."""
        api_key = SecretStore(db).get("linear_api_key")
        if not api_key:
            return None
        return cls(api_key)

    async def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {"query": query, "variables": variables or {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    response = await client.post(self.endpoint, headers=headers, json=payload)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if _is_final_attempt(attempt):
                        raise LinearGraphQLError(
                            "Linear GraphQL request failed after 3 attempts due to a "
                            "network error."
                        ) from exc
                    await asyncio.sleep(_retry_delay(attempt))
                    continue

                if _is_retryable_status(response.status_code):
                    if _is_final_attempt(attempt):
                        raise LinearGraphQLError(
                            "Linear GraphQL request failed after 3 attempts with "
                            f"HTTP {response.status_code}."
                        ) from _status_error(response)
                    await asyncio.sleep(_retry_delay(attempt, response))
                    continue

                break
        response.raise_for_status()
        body = response.json()
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

    async def list_teams(self) -> list[dict[str, Any]]:
        data = await self.execute(
            """
            query Teams {
              teams(first: 100) {
                nodes {
                  id
                  name
                  key
                }
              }
            }
            """
        )
        return _connection_nodes(data.get("teams"))

    async def list_projects(self, team_id: str) -> list[dict[str, Any]]:
        try:
            data = await self.execute(
                """
                query TeamProjects($teamId: String!) {
                  team(id: $teamId) {
                    projects(first: 100) {
                      nodes {
                        id
                        name
                      }
                    }
                  }
                }
                """,
                {"teamId": team_id},
            )
            team = data.get("team") if isinstance(data.get("team"), dict) else {}
            return _connection_nodes(cast(dict[str, Any], team).get("projects"))
        except LinearGraphQLError:
            data = await self.execute(
                """
                query Projects {
                  projects(first: 100) {
                    nodes {
                      id
                      name
                      teams {
                        nodes {
                          id
                        }
                      }
                    }
                  }
                }
                """
            )
            projects = _connection_nodes(data.get("projects"))
            return [
                project
                for project in projects
                if any(
                    team.get("id") == team_id for team in _connection_nodes(project.get("teams"))
                )
            ]

    async def list_team_states(self, team_id: str) -> list[dict[str, Any]]:
        data = await self.execute(
            """
            query TeamStates($teamId: String!) {
              team(id: $teamId) {
                states(first: 100) {
                  nodes {
                    id
                    name
                    type
                  }
                }
              }
            }
            """,
            {"teamId": team_id},
        )
        team = data.get("team") if isinstance(data.get("team"), dict) else {}
        return _connection_nodes(cast(dict[str, Any], team).get("states"))

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

        data = await self.execute(
            """
            query Issues($filter: IssueFilter) {
              issues(first: 100, filter: $filter) {
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
              }
            }
            """,
            {"filter": issue_filter},
        )
        return _connection_nodes(data.get("issues"))

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


def _is_final_attempt(attempt: int) -> bool:
    return attempt >= _MAX_ATTEMPTS - 1


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        parsed_retry_after = _parse_retry_after(retry_after)
        if parsed_retry_after is not None:
            return parsed_retry_after
    return float(min(_INITIAL_RETRY_DELAY_SECONDS * (2**attempt), _MAX_RETRY_DELAY_SECONDS))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


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
