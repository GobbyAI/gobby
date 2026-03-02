"""Tool filtering service based on workflow step restrictions."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.workflows.constants import EXEMPT_TOOLS
from gobby.workflows.definitions import WorkflowDefinition

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase
    from gobby.workflows.loader import WorkflowLoader
    from gobby.workflows.state_manager import WorkflowStateManager

logger = logging.getLogger("gobby.mcp.tool_filter")


class ToolFilterService:
    """
    Service to filter tools based on workflow step restrictions.

    When a session has an active step-based workflow, this service
    filters the tool list to only include tools allowed in the current step.
    """

    def __init__(
        self,
        db: "LocalDatabase | None" = None,
        loader: "WorkflowLoader | None" = None,
        state_manager: "WorkflowStateManager | None" = None,
    ):
        self._db = db
        self._loader = loader
        self._state_manager = state_manager

    def _get_state_manager(self) -> "WorkflowStateManager | None":
        """Lazy initialization of state manager."""
        if self._state_manager:
            return self._state_manager

        if self._db:
            from gobby.workflows.state_manager import WorkflowStateManager

            self._state_manager = WorkflowStateManager(self._db)
            return self._state_manager

        return None

    def _get_loader(self) -> "WorkflowLoader | None":
        """Lazy initialization of workflow loader."""
        if self._loader:
            return self._loader

        from gobby.workflows.loader import WorkflowLoader

        self._loader = WorkflowLoader(db=self._db)
        return self._loader

    def get_step_restrictions(
        self,
        session_id: str,
        project_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """
        Get tool restrictions for the current workflow step.

        Args:
            session_id: Session ID to check
            project_path: Optional project path for loading workflow

        Returns:
            Dict with allowed_tools and blocked_tools, or None if no workflow active
        """
        state_manager = self._get_state_manager()
        if not state_manager:
            logger.debug("No state manager available for tool filtering")
            return None

        state = state_manager.get_state(session_id)
        if not state:
            logger.debug(f"No workflow state for session {session_id}")
            return None

        loader = self._get_loader()
        if not loader:
            logger.debug("No workflow loader available")
            return None

        proj = Path(project_path) if project_path else None
        definition = loader.load_workflow_sync(state.workflow_name, proj)
        if not definition:
            logger.warning(f"Workflow '{state.workflow_name}' not found")
            return None

        # Tool filtering only applies to step-based workflows
        if not isinstance(definition, WorkflowDefinition):
            logger.debug(f"Workflow '{state.workflow_name}' is not a step-based workflow")
            return None

        step = definition.get_step(state.step)
        if not step:
            logger.warning(f"Step '{state.step}' not found in workflow '{state.workflow_name}'")
            return None

        return {
            "workflow_name": state.workflow_name,
            "step": state.step,
            "allowed_tools": step.allowed_tools,
            "blocked_tools": step.blocked_tools,
        }

    def is_tool_allowed(
        self,
        tool_name: str,
        session_id: str,
        project_path: str | Path | None = None,
    ) -> tuple[bool, str | None]:
        """
        Check if a tool is allowed in the current workflow step.

        Args:
            tool_name: Name of the tool to check
            session_id: Session ID
            project_path: Optional project path

        Returns:
            Tuple of (is_allowed, reason). If no workflow is active, returns (True, None).
        """
        # Exempt tools (MCP discovery) are always allowed
        if tool_name in EXEMPT_TOOLS:
            return True, None

        restrictions = self.get_step_restrictions(session_id, project_path)
        if not restrictions:
            return True, None

        # Check blocked list first
        if tool_name in restrictions["blocked_tools"]:
            return False, f"Tool '{tool_name}' is blocked in step '{restrictions['step']}'"

        # Check allowed list
        allowed = restrictions["allowed_tools"]
        if allowed == "all":
            return True, None

        if tool_name not in allowed:
            return (
                False,
                f"Tool '{tool_name}' is not in allowed list for step '{restrictions['step']}'",
            )

        return True, None

    def filter_tools(
        self,
        tools: list[dict[str, Any]],
        session_id: str | None = None,
        project_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """
        Filter a list of tools based on workflow step restrictions.

        Args:
            tools: List of tool dicts with at least a 'name' key
            session_id: Session ID to check for workflow state
            project_path: Optional project path

        Returns:
            Filtered list of tools. If no session_id or no active workflow,
            returns the original list unchanged.
        """
        if not session_id:
            return tools

        restrictions = self.get_step_restrictions(session_id, project_path)
        if not restrictions:
            return tools

        allowed = restrictions["allowed_tools"]
        blocked = restrictions["blocked_tools"]

        filtered = []
        for tool in tools:
            name = tool.get("name", "")

            # Always include exempt tools (MCP discovery)
            if name in EXEMPT_TOOLS:
                filtered.append(tool)
                continue

            # Skip blocked tools
            if name in blocked:
                logger.debug(f"Filtering out blocked tool: {name}")
                continue

            # Check allowed list
            if allowed != "all" and name not in allowed:
                logger.debug(f"Filtering out non-allowed tool: {name}")
                continue

            filtered.append(tool)

        if len(filtered) < len(tools):
            logger.info(
                f"Filtered {len(tools) - len(filtered)} tools based on step '{restrictions['step']}'"
            )

        return filtered

    def filter_servers_tools(
        self,
        servers: list[dict[str, Any]],
        session_id: str | None = None,
        project_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """
        Filter tools from multiple servers based on workflow step restrictions.

        Args:
            servers: List of server dicts with 'name' and 'tools' keys
            session_id: Session ID to check for workflow state
            project_path: Optional project path

        Returns:
            Servers list with filtered tools. Empty servers are kept but with empty tool lists.
        """
        if not session_id:
            return servers

        restrictions = self.get_step_restrictions(session_id, project_path)
        if not restrictions:
            return servers

        result = []
        for server in servers:
            server_name = server.get("name", "")
            tools = server.get("tools", [])

            filtered_tools = self.filter_tools(tools, session_id, project_path)

            result.append(
                {
                    "name": server_name,
                    "tools": filtered_tools,
                }
            )

        return result
