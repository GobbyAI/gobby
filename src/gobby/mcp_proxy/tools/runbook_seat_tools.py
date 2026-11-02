"""gobby-agents runbook tools: the read-only seat guard (#23329) and team admission."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gobby.agents.runbook_seats import RunbookSeatRefusal, RunbookSeatStores, check_runbook_seats
from gobby.agents.team_admission_records import TeamAdmissionRefused
from gobby.agents.team_lifecycle import admit_team
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.project_checkouts import require_root
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.utils.machine_id import require_machine_id


def runbook_seat_stores(db: HubDatabase) -> RunbookSeatStores:
    """Build the guard's stores over one hub database."""
    return RunbookSeatStores(
        sessions=SessionManager(db),
        executions=lambda project_id: LocalPipelineExecutionManager(db, project_id),
        definitions=AgentDefinitionManager(db),
        workspaces=WorkspaceManager(db),
        runs=LocalAgentRunManager(db),
        tasks=LocalTaskManager(db),
    )


def register_runbook_seat_tools(registry: InternalToolRegistry, ctx: AgentsRegistryContext) -> None:
    @registry.tool(
        name="check_runbook_seats",
        description=(
            "Read-only runbook seat guard, the runbook's first step. Call it from a pipeline "
            "mcp step. requested is comma-separated seat names; catalogue is a list of "
            "{name, title, agent}. Refuses when the same runbook is still launching for the "
            "same project in the same workspace on the same machine, or when a seat's agent "
            "definition is missing or disabled. report_to, if supplied, is a session ref that "
            "must be live and whose definition admits every seat launched; require_report_to "
            "rejects omission. "
            "launch names the seats to start; skip_live_seats leaves out each seat whose "
            "agent already runs on this machine with a live session in this project and "
            "maps it to that session under live. tab_title names the runbook tab; with task it "
            "becomes '<tab_title> - #<seq>', and tab_exists reports that tab of the workspace. "
            "team_id limits live seats to that admitted team's runs and maps "
            "every catalogue seat with such a run under live, requested or not. "
            "seat_models maps a seat name to the model its launch takes; a seat that launches "
            "with no model there and none on its agent definition is refused. "
            "On success it returns the checked seats."
        ),
        read_only=True,
    )
    def check_runbook_seats_tool(
        requested: str,
        catalogue: list[dict[str, Any]],
        workspace: str,
        report_to: str | None = None,
        require_report_to: bool = False,
        skip_live_seats: bool = False,
        team_id: str | None = None,
        tab_title: str | None = None,
        task: str | None = None,
        seat_models: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        if ctx.db is None or not caller_ref:
            return {
                "success": False,
                "error": "check_runbook_seats needs a database and a caller session",
            }
        try:
            caller_session_id = ctx.resolve_session_id(caller_ref)
            report_session = (
                ctx.resolve_session_id(report_to)
                if report_to is not None and report_to.strip()
                else report_to
            )
            admitted = check_runbook_seats(
                runbook_seat_stores(ctx.db),
                caller_session_id=caller_session_id,
                workspace=workspace,
                requested=requested,
                catalogue=catalogue,
                report_to=report_session,
                require_report_to=require_report_to,
                skip_live_seats=skip_live_seats,
                team_id=team_id,
                tab_title=tab_title,
                task=task,
                seat_models=seat_models,
            )
        except RunbookSeatRefusal as exc:
            return {"success": False, "error": str(exc), "error_code": "runbook_seat_refused"}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        # No "error" key on success: execute_mcp_step fails any result that carries one.
        return {"success": True, **asdict(admitted)}

    @registry.tool(
        name="admit_team",
        description=(
            "Admit a single-use team for one open task. Call it from a team runbook's "
            "'admit' pipeline mcp step. Under the project landing lock it creates the task's "
            "branch and worktree from the integration branch, both named "
            "task-<seq>-<title-slug>; it accepts no branch or tree name. A re-run of the same "
            "execution returns the same team. A replacement execution passes team_id, the "
            "admitting execution id, and must name the same task. Refuses team_exists while "
            "the task has a live team tree. "
            "checkout_mode=none admits a treeless team with no branch or tree; its seats run "
            "in the main checkout, and a re-admit for the task joins it. Returns the "
            "team plus task_id, branch, worktree and project_path for the seat steps. "
            "Pass the seat guard's resolved tab_title to bind later team seats to that tab."
        ),
    )
    async def admit_team_tool(
        task: str,
        team_id: str | None = None,
        parent_team_id: str | None = None,
        reviewer_agent: str | None = None,
        checkout_mode: str = "worktree",
        tab_title: str | None = None,
    ) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        if ctx.db is None or not caller_ref:
            return {
                "success": False,
                "error": "admit_team needs a database and a caller session",
                "error_code": "team_caller_invalid",
            }
        try:
            team = await admit_team(
                ctx.db,
                caller_session_id=ctx.resolve_session_id(caller_ref),
                task=task,
                team_id=team_id,
                parent_team_id=parent_team_id,
                reviewer_agent=reviewer_agent,
                checkout_mode=checkout_mode,
                tab_title=tab_title,
            )
        except TeamAdmissionRefused as exc:
            return {"success": False, "error": str(exc), "error_code": exc.code}
        except ValueError as exc:
            return {"success": False, "error": str(exc), "error_code": "team_caller_invalid"}
        return {
            "success": True,
            "team": asdict(team),
            "task_id": team.task_id,
            "branch": team.branch,
            "worktree": team.worktree_id,
            "project_path": team.worktree_path,
            "main_project_path": require_root(ctx.db, team.project_id, require_machine_id()),
        }
