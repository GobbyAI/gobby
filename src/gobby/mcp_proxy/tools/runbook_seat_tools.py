"""gobby-agents:check_runbook_seats, the runbook's read-only seat guard (#23329)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gobby.agents.runbook_seats import RunbookSeatRefusal, RunbookSeatStores, check_runbook_seats
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import (
    _count_active_agents,
    max_active_agents_for_project,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workspaces import WorkspaceManager


def runbook_seat_stores(db: HubDatabase, *, project_path: str) -> RunbookSeatStores:
    """Build the guard's stores; free slots use the same cap and count as ``reserve_agent_slot``."""

    def free_slots(project_id: str) -> int:
        return max_active_agents_for_project(project_path) - _count_active_agents(db, project_id)

    return RunbookSeatStores(
        sessions=SessionManager(db),
        executions=lambda project_id: LocalPipelineExecutionManager(db, project_id),
        runs=LocalAgentRunManager(db),
        definitions=AgentDefinitionManager(db),
        workspaces=WorkspaceManager(db),
        free_slots=free_slots,
    )


def register_runbook_seat_tools(registry: InternalToolRegistry, ctx: AgentsRegistryContext) -> None:
    @registry.tool(
        name="check_runbook_seats",
        description=(
            "Read-only runbook seat guard, the runbook's first step. Call it from a pipeline "
            "mcp step. requested is comma-separated seat names; catalogue is a list of "
            "{name, title, agent}. Refuses on a live sibling execution, a requested seat "
            "held by an active run, a missing or disabled agent definition, or too few "
            "free agent slots. On success it returns the checked seats and free_slots."
        ),
        read_only=True,
    )
    def check_runbook_seats_tool(
        workspace: str, requested: str, catalogue: list[dict[str, Any]]
    ) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        project_path = (ctx.get_project_context() or {}).get("project_path")
        if ctx.db is None or not caller_ref or not isinstance(project_path, str):
            return {
                "success": False,
                "error": "check_runbook_seats needs a database, a caller session and a project",
            }
        try:
            admitted = check_runbook_seats(
                runbook_seat_stores(ctx.db, project_path=project_path),
                caller_session_id=ctx.resolve_session_id(caller_ref),
                workspace=workspace,
                requested=requested,
                catalogue=catalogue,
            )
        except (RunbookSeatRefusal, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        # No "error" key on success: execute_mcp_step fails any result that carries one.
        return {"success": True, **asdict(admitted)}
