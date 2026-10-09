"""gobby-agents:check_runbook_seats, the runbook's read-only seat guard (#23329)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gobby.agents.crew_lane_inputs import resolve_crew_lane_inputs
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
            "session ref; require_report_to rejects omission without lane inference. "
            "For crew-lane, lane resolves omitted report_to, workspace, lane_pane and "
            "worktree from the shared roster and live state, and omitted lane_columns and "
            "lane_rows from the gclient window showing the workspace; explicit values, "
            "names and UUIDs override. "
            "Ambiguous state refuses with candidates. On success it returns the checked "
            "seats and resolved launch inputs."
        ),
        read_only=True,
    )
    def check_runbook_seats_tool(
        requested: str,
        catalogue: list[dict[str, Any]],
        workspace: str | None = None,
        report_to: str | None = None,
        require_report_to: bool = False,
        lane: str | int | None = None,
        worktree: str | None = None,
        lane_pane: str | None = None,
        lane_columns: int | None = None,
        lane_rows: int | None = None,
    ) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        if ctx.db is None or not caller_ref:
            return {
                "success": False,
                "error": "check_runbook_seats needs a database and a caller session",
            }
        try:
            caller_session_id = ctx.resolve_session_id(caller_ref)
            resolved: dict[str, Any] = {}
            if lane is not None:
                leases = ctx.lease_registry_resolver() if ctx.lease_registry_resolver else None
                resolved = asdict(
                    resolve_crew_lane_inputs(
                        ctx.db,
                        caller_session_id=caller_session_id,
                        lane=lane,
                        workspace=workspace,
                        report_to=report_to,
                        worktree=worktree,
                        lane_pane=lane_pane,
                        lane_columns=lane_columns,
                        lane_rows=lane_rows,
                        viewports=None if leases is None else leases.viewports,
                    )
                )
                workspace = resolved["workspace"]
                report_to = resolved["report_to"]
            if workspace is None:
                raise RunbookSeatRefusal("workspace is required without a lane")
            admitted = check_runbook_seats(
                runbook_seat_stores(ctx.db),
                caller_session_id=caller_session_id,
                workspace=workspace,
                requested=requested,
                catalogue=catalogue,
                report_to=report_to,
                require_report_to=require_report_to,
            )
        except RunbookSeatRefusal as exc:
            return {"success": False, "error": str(exc), "error_code": "runbook_seat_refused"}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        # No "error" key on success: execute_mcp_step fails any result that carries one.
        return {"success": True, **asdict(admitted), **resolved}
