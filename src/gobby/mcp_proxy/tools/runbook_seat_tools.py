"""gobby-agents:check_runbook_seats, the runbook's read-only seat guard (#23329)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gobby.agents.runbook_seats import RunbookSeatRefusal, RunbookSeatStores, check_runbook_seats
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workspaces import WorkspaceManager


def runbook_seat_stores(db: HubDatabase) -> RunbookSeatStores:
    """Build the guard's stores over one hub database."""
    return RunbookSeatStores(
        sessions=SessionManager(db),
        executions=lambda project_id: LocalPipelineExecutionManager(db, project_id),
        definitions=AgentDefinitionManager(db),
        workspaces=WorkspaceManager(db),
    )


def register_runbook_seat_tools(registry: InternalToolRegistry, ctx: AgentsRegistryContext) -> None:
    @registry.tool(
        name="check_runbook_seats",
        description=(
            "Read-only runbook seat guard, the runbook's first step. Call it from a pipeline "
            "mcp step. requested is comma-separated seat names; catalogue is a list of "
            "{name, title, agent}. Refuses when the same runbook is still launching for the "
            "same project in the same workspace on the same machine, or when a seat's agent "
            "definition is missing or disabled. If supplied, report_to must be a nonempty "
            "session ref; require_report_to also rejects omission. On success it returns "
            "the checked seats."
        ),
        read_only=True,
    )
    def check_runbook_seats_tool(
        workspace: str,
        requested: str,
        catalogue: list[dict[str, Any]],
        report_to: str | None = None,
        require_report_to: bool = False,
    ) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        if ctx.db is None or not caller_ref:
            return {
                "success": False,
                "error": "check_runbook_seats needs a database and a caller session",
            }
        try:
            admitted = check_runbook_seats(
                runbook_seat_stores(ctx.db),
                caller_session_id=ctx.resolve_session_id(caller_ref),
                workspace=workspace,
                requested=requested,
                catalogue=catalogue,
                report_to=report_to,
                require_report_to=require_report_to,
            )
        except (RunbookSeatRefusal, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        # No "error" key on success: execute_mcp_step fails any result that carries one.
        return {"success": True, **asdict(admitted)}
