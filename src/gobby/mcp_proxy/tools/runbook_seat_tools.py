"""gobby-agents runbook tools: the read-only seat guard (#23329) and team admission."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gobby.agents.runbook_seats import RunbookSeatRefusal, RunbookSeatStores, check_runbook_seats
from gobby.agents.team_lifecycle import TeamAdmissionRefused, admit_team
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workspaces import WorkspaceManager


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
            "definition is missing or disabled. If supplied, report_to must be a nonempty "
            "session ref; require_report_to rejects omission. "
            "launch names the seats to start; skip_live_seats leaves out each seat whose "
            "agent already runs on this machine with a live session in this project and "
            "maps it to that session under live. tab_title sets tab_exists for that tab of "
            "the workspace. team_id limits live seats to that admitted team's runs and maps "
            "every catalogue seat with such a run under live, requested or not. "
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
    ) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        if ctx.db is None or not caller_ref:
            return {
                "success": False,
                "error": "check_runbook_seats needs a database and a caller session",
            }
        try:
            caller_session_id = ctx.resolve_session_id(caller_ref)
            admitted = check_runbook_seats(
                runbook_seat_stores(ctx.db),
                caller_session_id=caller_session_id,
                workspace=workspace,
                requested=requested,
                catalogue=catalogue,
                report_to=report_to,
                require_report_to=require_report_to,
                skip_live_seats=skip_live_seats,
                team_id=team_id,
                tab_title=tab_title,
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
            "the task has a live team tree. fresh_reviewer=true joins team_id as the review "
            "epoch's one fresh reviewer; it requires exactly two rejected review rounds, "
            "exclude_reviewers (comma-separated session refs) naming every session that "
            "reviewed in the epoch, and the claim already handed to one of them. Returns the "
            "team plus task_id, branch, worktree and project_path for the seat steps."
        ),
    )
    async def admit_team_tool(
        task: str,
        team_id: str | None = None,
        fresh_reviewer: bool = False,
        exclude_reviewers: str | None = None,
        reviewer_agent: str | None = None,
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
                fresh_reviewer=fresh_reviewer,
                exclude_reviewers=[
                    ref.strip() for ref in (exclude_reviewers or "").split(",") if ref.strip()
                ],
                reviewer_agent=reviewer_agent,
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
        }
