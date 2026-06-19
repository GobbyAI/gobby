"""Project and Linear discovery operations for Linear sync."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.integrations.linear_graphql import LinearGraphQLClient
from gobby.sync.linear_support import (
    LinearSyncError,
    _extract_record,
    _extract_records,
    decorate_issue_result,
    linear_issue_title,
    task_ref,
)
from gobby.utils.project_init import update_project_json_fields

if TYPE_CHECKING:
    from gobby.integrations.linear import LinearIntegration
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager


class LinearProjectOpsMixin:
    """Linear project binding and discovery operations shared by the sync service."""

    mcp_manager: MCPClientManager
    task_manager: LocalTaskManager
    project_id: str
    linear_team_id: str | None
    linear_project_id: str | None
    linear: LinearIntegration
    _project_manager: LocalProjectManager | None

    @property
    def project_manager(self) -> LocalProjectManager:
        """Lazy-init project manager from task_manager's db if not provided."""
        if self._project_manager is None:
            from gobby.storage.projects import LocalProjectManager

            self._project_manager = LocalProjectManager(self.task_manager.db)
        return self._project_manager

    def _get_project_synced_at(self) -> str | None:
        """Get the project's linear_synced_at cursor."""
        project = self.project_manager.get(self.project_id)
        return project.linear_synced_at if project else None

    def _get_linear_project_id(self) -> str | None:
        """Get the Linear project binding, preferring the service override."""
        if self.linear_project_id:
            return self.linear_project_id
        project = self.project_manager.get(self.project_id)
        return project.linear_project_id if project else None

    async def _get_graphql_client(self) -> LinearGraphQLClient | None:
        return await LinearGraphQLClient.from_database_async(self.task_manager.db)

    def _linear_mcp_has_tool(self, tool_name: str) -> bool:
        """Return False only when cached Linear tool metadata proves absence."""
        configs = getattr(self.mcp_manager, "server_configs", None)
        if not isinstance(configs, list):
            return True
        for config in configs:
            if getattr(config, "name", None) != "linear":
                continue
            tools = getattr(config, "tools", None)
            if tools is None:
                return True
            return any(tool.get("name") == tool_name for tool in tools if isinstance(tool, dict))
        return True

    def _linear_project_name(self) -> str:
        project = self.project_manager.get(self.project_id)
        if not project:
            raise LinearSyncError(f"Project not found: {self.project_id}")
        return Path(project.repo_path).name if project.repo_path else project.name

    def _linear_project_display_name(self) -> str | None:
        try:
            return self._linear_project_name()
        except Exception:
            return None

    def _task_ref(self, task: Any) -> str:
        return task_ref(task)

    def _linear_issue_title(self, task: Any) -> str:
        return linear_issue_title(task)

    def _decorate_issue_result(
        self,
        result: dict[str, Any],
        task: Any,
        *,
        team_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        return decorate_issue_result(
            result,
            task,
            team_id=team_id,
            project_id=project_id,
            project_name=self._linear_project_display_name(),
        )

    async def _linear_state_id_for_name(
        self,
        client: LinearGraphQLClient,
        team_id: str | None,
        state_name: str | None,
    ) -> str | None:
        if not team_id or not state_name:
            return None
        states = await client.list_team_states(team_id)
        for state in states:
            if state.get("name") == state_name:
                state_id = state.get("id")
                return state_id if isinstance(state_id, str) else None
        return None

    def _update_synced_at(self, timestamp: str | None = None) -> None:
        """Update the project's linear_synced_at cursor."""
        ts = timestamp or datetime.now(UTC).isoformat()
        self.project_manager.update(self.project_id, linear_synced_at=ts)

    async def list_teams(self) -> list[dict[str, Any]]:
        """List Linear teams available to the configured MCP auth."""
        self.linear.require_available()
        try:
            if not self._linear_mcp_has_tool("list_teams"):
                raise LinearSyncError("Linear MCP server does not expose list_teams.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="list_teams",
                arguments={},
            )
        except Exception as e:
            client = await self._get_graphql_client()
            if client:
                return await client.list_teams()
            raise LinearSyncError(
                "Linear MCP server does not expose list_teams and no Linear API key "
                "is available for GraphQL discovery."
            ) from e
        return _extract_records(result, "teams")

    async def list_projects(self, team_id: str) -> list[dict[str, Any]]:
        """List Linear projects for a team."""
        self.linear.require_available()
        try:
            if not self._linear_mcp_has_tool("list_projects"):
                raise LinearSyncError("Linear MCP server does not expose list_projects.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="list_projects",
                arguments={"teamId": team_id},
            )
        except Exception as e:
            client = await self._get_graphql_client()
            if client:
                return await client.list_projects(team_id)
            raise LinearSyncError(
                "Linear MCP server does not expose list_projects and no Linear API key "
                "is available for GraphQL project discovery."
            ) from e
        return _extract_records(result, "projects")

    async def ensure_linear_project(
        self,
        team_id: str,
        project_name: str,
        project_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Return an existing Linear project by name, or create it."""
        if project_id:
            return {"id": project_id, "name": project_name}, False

        for project in await self.list_projects(team_id):
            if project.get("name") == project_name:
                return project, False

        try:
            if not self._linear_mcp_has_tool("create_project"):
                raise LinearSyncError("Linear MCP server does not expose create_project.")
            result = await self.mcp_manager.call_tool(
                server_name="linear",
                tool_name="create_project",
                arguments={"teamId": team_id, "name": project_name},
            )
            project = _extract_record(result, "project")
        except Exception as e:
            client = await self._get_graphql_client()
            if not client:
                raise LinearSyncError(
                    "Linear MCP server does not expose create_project and no Linear API key "
                    "is available for GraphQL project creation."
                ) from e
            project = await client.create_project(team_id, project_name)
        if not project.get("id"):
            raise LinearSyncError("Linear MCP create_project did not return a project id.")
        return project, True

    async def ensure_project_binding(self, team_id: str) -> str:
        """Ensure this Gobby project is bound to a same-named Linear project."""
        linear_project_id = self._get_linear_project_id()
        if linear_project_id:
            return linear_project_id

        project_name = self._linear_project_name()
        linear_project, _ = await self.ensure_linear_project(team_id, project_name)
        resolved_project_id = linear_project.get("id")
        if not isinstance(resolved_project_id, str) or not resolved_project_id:
            raise LinearSyncError("Linear project setup did not return a project id.")

        self.linear_project_id = resolved_project_id
        updated = self.project_manager.update(
            self.project_id,
            linear_team_id=team_id,
            linear_project_id=resolved_project_id,
        )
        if updated and updated.repo_path:
            update_project_json_fields(
                Path(updated.repo_path),
                linear_team_id=team_id,
                linear_project_id=resolved_project_id,
            )
        return resolved_project_id

    def _issue_list_args(
        self,
        team_id: str,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"teamId": team_id}
        linear_project_id = self._get_linear_project_id()
        if linear_project_id:
            args["projectId"] = linear_project_id
        if state:
            args["state"] = state
        if labels:
            args["labels"] = labels
        return args
