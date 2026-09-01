"""Project and Linear discovery operations for Linear sync."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.integrations.linear_graphql import LinearGraphQLClient
from gobby.mcp_proxy.models import MCPError
from gobby.sync.linear_support import (
    LinearSyncError,
    _extract_record,
    _extract_records,
    decorate_issue_result,
    linear_issue_title,
    task_ref,
)
from gobby.utils.datetime import datetime_to_iso
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

    def _linear_server_id(self) -> str:
        from gobby.mcp_proxy.services.server_resolution import resolved_server_id

        server_id = resolved_server_id(self.mcp_manager, "linear", project_id=self.project_id)
        if server_id is None:
            raise LinearSyncError("Linear MCP server not found")
        return server_id

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
        return datetime_to_iso(project.linear_synced_at) if project else None

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
        from gobby.storage.project_checkouts import require_root
        from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
        from gobby.storage.workspace_machine_scope import require_local_machine_id

        project = self.project_manager.get(self.project_id)
        if not project:
            raise LinearSyncError(f"Project not found: {self.project_id}")
        if project.id in CHECKOUT_FREE_PROJECT_IDS:
            return project.name
        machine_id = require_local_machine_id(
            None, resource_kind="project_checkout", resource_id=project.id
        )
        return Path(require_root(self.project_manager.db, project.id, machine_id)).name

    def _linear_project_display_name(self) -> str | None:
        try:
            return self._linear_project_name()
        except LinearSyncError:
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
        state_ids_by_team: dict[str, dict[str, str]] | None = None,
    ) -> str | None:
        if not team_id or not state_name:
            return None

        state_ids = state_ids_by_team.get(team_id) if state_ids_by_team is not None else None
        if state_ids is None:
            states = await client.list_team_states(team_id)
            state_ids = {
                name: state_id
                for state in states
                if isinstance((name := state.get("name")), str)
                and isinstance((state_id := state.get("id")), str)
            }
            if state_ids_by_team is not None:
                state_ids_by_team[team_id] = state_ids
        return state_ids.get(state_name)

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
                self._linear_server_id(),
                tool_name="list_teams",
                arguments={},
            )
        except LinearSyncError as e:
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
                self._linear_server_id(),
                tool_name="list_projects",
                arguments={"teamId": team_id},
            )
        except LinearSyncError as e:
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

        if self._linear_mcp_has_tool("create_project"):
            create_failure_message = (
                "Linear MCP create_project failed and no Linear API key "
                "is available for GraphQL project creation."
            )
            try:
                result = await self.mcp_manager.call_tool(
                    self._linear_server_id(),
                    tool_name="create_project",
                    arguments={"teamId": team_id, "name": project_name},
                )
                project = _extract_record(result, "project")
                mcp_project_id = project.get("id")
                if not isinstance(mcp_project_id, str) or not mcp_project_id.strip():
                    raise LinearSyncError("Linear MCP create_project did not return a project id.")
                return project, True
            except (LinearSyncError, MCPError) as e:
                mcp_error = e
        else:
            mcp_error = LinearSyncError("Linear MCP server does not expose create_project.")
            create_failure_message = (
                "Linear MCP server does not expose create_project and no Linear API key "
                "is available for GraphQL project creation."
            )

        for project in await self.list_projects(team_id):
            if project.get("name") == project_name:
                return project, False

        client = await self._get_graphql_client()
        if not client:
            raise LinearSyncError(create_failure_message) from mcp_error
        project = await client.create_project(team_id, project_name)
        graphql_project_id = project.get("id")
        if not isinstance(graphql_project_id, str) or not graphql_project_id.strip():
            raise LinearSyncError("Linear GraphQL create_project did not return a project id.")
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
        if updated:
            from gobby.storage.project_checkouts import require_root
            from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
            from gobby.storage.workspace_machine_scope import require_local_machine_id

            if self.project_id not in CHECKOUT_FREE_PROJECT_IDS:
                machine_id = require_local_machine_id(
                    None, resource_kind="project_checkout", resource_id=self.project_id
                )
                update_project_json_fields(
                    Path(require_root(self.project_manager.db, self.project_id, machine_id)),
                    linear_team_id=team_id,
                    linear_project_id=resolved_project_id,
                )
        return resolved_project_id

    def _issue_list_args(
        self,
        team_id: str,
        state: str | None = None,
        labels: list[str] | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"teamId": team_id, "limit": 100}
        linear_project_id = self._get_linear_project_id()
        if linear_project_id:
            args["projectId"] = linear_project_id
        if state:
            args["state"] = state
        if labels:
            args["labels"] = labels
        if cursor:
            args["cursor"] = cursor
        return args
