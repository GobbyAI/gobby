"""Collapse a closed task's team: end its seats, then delete its tree safely (plan 3.3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.agents.agent_workspaces import release_epic_workspace, schedule_workspace_commit
from gobby.agents.team_lifecycle import (
    TeamBinding,
    _admission_step,
    _binding,
    _merge,
    _recorded,
)
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.daemon_resume_keys import CONSUMED_AT_KEY, CONSUMED_BY_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.storage.tasks import LocalTaskManager, TaskAlreadyEscalatedError
from gobby.storage.team_records import ADMISSION_STEP
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline_state import StepExecution
from gobby.worktrees.deletion import WorktreeDeletionResult

logger = logging.getLogger(__name__)

TEARDOWN_KEY = "teardown"
# The closing manager receives its close response before any seat ends.
TEARDOWN_GRACE = timedelta(seconds=30)
FINDING_LABEL = "team-teardown"

TeamRunTerminator = Callable[[AgentRun], Awaitable[bool]]
TeamTreeDeleter = Callable[[TeamBinding], Awaitable[WorktreeDeletionResult]]


@dataclass(frozen=True)
class _DueTeam:
    binding: TeamBinding
    executions: list[str]  # the team's own execution and every execution that joined it


async def reconcile_team_teardowns(
    db: HubDatabase,
    *,
    terminate: TeamRunTerminator,
    delete: TeamTreeDeleter,
    now: datetime | None = None,
    grace: timedelta = TEARDOWN_GRACE,
) -> int:
    """One stateless pass over every closed task's team; returns the actions taken."""
    cutoff = (now or datetime.now(UTC)) - grace
    actions = 0
    for team in await asyncio.to_thread(_due_teams, db, cutoff):
        try:
            actions += await _tear_down(db, team, terminate, delete)
        except Exception:
            logger.exception("Team %s teardown failed; the next pass retries", team.binding.team_id)
    return actions


def _due_teams(db: HubDatabase, cutoff: datetime) -> list[_DueTeam]:
    """Teams whose task closed before ``cutoff`` and whose own record has no outcome yet.

    Only the daemon on the tree's machine can prove and delete it, so a tree recorded
    on another machine is left to that daemon.
    """
    rows = db.fetchall(
        """
SELECT se.* FROM step_executions se
JOIN tasks t ON t.id::text = se.output_json #>> '{team,task_id}'
LEFT JOIN worktrees w ON w.id::text = se.output_json #>> '{team,worktree_id}'
WHERE se.step_id = %s
AND se.output_json #>> '{team,team_id}' = se.execution_id::text
AND se.output_json #>> '{teardown,outcome}' IS NULL
AND t.closed_at <= %s
AND (w.id IS NULL OR w.machine_id = %s)
ORDER BY se.id
""",
        (ADMISSION_STEP, cutoff, require_machine_id()),
    )
    due = [b for b in (_binding(_recorded(StepExecution.from_row(row))) for row in rows) if b]
    if not due:
        return []
    members: dict[str, list[str]] = {binding.team_id: [] for binding in due}
    for row in db.fetchall(
        "SELECT execution_id, output_json #>> '{team,team_id}' AS team_id FROM step_executions "
        "WHERE step_id = %s AND output_json #>> '{team,team_id}' = ANY(%s) ORDER BY id",
        (ADMISSION_STEP, list(members)),
    ):
        members[row["team_id"]].append(str(row["execution_id"]))
    return [_DueTeam(binding, members[binding.team_id]) for binding in due]


async def _tear_down(
    db: HubDatabase, team: _DueTeam, terminate: TeamRunTerminator, delete: TeamTreeDeleter
) -> int:
    folder = await asyncio.to_thread(release_epic_workspace, db, team.binding)
    runs = await asyncio.to_thread(_live_runs, db, team)
    ended = [await terminate(run) for run in runs]
    if not all(ended):
        logger.warning("Team %s keeps live seats; the next pass retries", team.binding.team_id)
        return sum(ended)
    await asyncio.to_thread(_expire_seats, db, team)
    if folder is not None:
        schedule_workspace_commit(team.binding.project_id, folder.name)
    result: WorktreeDeletionResult | None = await delete(team.binding)
    if result is not None and result.error_code == "worktree_not_eligible":
        result = await asyncio.to_thread(_ineligible, db, team.binding)
    if result is None or result.abandoned:
        return len(ended)  # a shutdown or another cleanup interrupted it; the next pass retries
    outcome = (
        {"outcome": "cleaned"}
        if result.success
        else await asyncio.to_thread(_refuse, db, team.binding, result)
    )
    await asyncio.to_thread(_record, db, team.binding, outcome)
    return len(ended) + 1


def _live_runs(db: HubDatabase, team: _DueTeam) -> list[AgentRun]:
    """Running and pending runs launched by any of the team's runbooks."""
    executions = LocalPipelineExecutionManager(db, team.binding.project_id)
    runs = LocalAgentRunManager(db)
    live: list[AgentRun] = []
    for execution_id in team.executions:
        execution = executions.get_execution(execution_id)
        if execution is not None and execution.session_id is not None:
            live.extend(runs.list_by_parent(execution.session_id))
    return live


def _expire_seats(db: HubDatabase, team: _DueTeam) -> None:
    """Expire every team seat's session a stop or cancel left live, and retire its resume.

    This covers runs already terminal before teardown (a daemon-stop parked seat),
    which ``_live_runs`` never sees. The parked resume is marked consumed by the
    teardown, so recovery skips it while other expired parked children stay resumable.
    """
    executions = LocalPipelineExecutionManager(db, team.binding.project_id)
    runbooks = [
        execution.session_id
        for execution in map(executions.get_execution, team.executions)
        if execution is not None and execution.session_id is not None
    ]
    if not runbooks:
        return
    retired = {CONSUMED_AT_KEY: datetime.now(UTC).isoformat(), CONSUMED_BY_KEY: TEARDOWN_KEY}
    db.execute(
        "UPDATE agent_runs SET resume_metadata_json = "
        "COALESCE(resume_metadata_json, '{}'::jsonb) || %s::jsonb "
        "WHERE parent_session_id = ANY(%s) AND status = 'cancelled' "
        "AND terminal_reason = 'daemon_stop' "
        "AND NOT (COALESCE(resume_metadata_json, '{}'::jsonb) ? %s)",
        (json.dumps(retired), runbooks, CONSUMED_AT_KEY),
    )
    rows = db.fetchall(
        "SELECT DISTINCT s.id::text AS id FROM agent_runs r "
        "JOIN sessions s ON s.id = r.child_session_id "
        "WHERE r.parent_session_id = ANY(%s) AND s.status = ANY(%s)",
        (runbooks, list(LIVE_SESSION_STATUS_ORDER)),
    )
    sessions = SessionManager(db)
    for row in rows:
        sessions.update_status(row["id"], "expired")


def _ineligible(db: HubDatabase, binding: TeamBinding) -> WorktreeDeletionResult | None:
    """Why the cleanup lock passed over the tree, or None when only a row lock held it."""
    tree = LocalWorktreeManager(db).get(binding.worktree_id)
    if tree is None:
        return WorktreeDeletionResult(success=True, found=False)  # deleted before a restart
    holder = (
        tree.agent_session_id
        or LocalTaskManager(db).get_task(binding.task_id).claimed_by_session_id
    )
    if holder is None:
        return None
    return WorktreeDeletionResult(
        success=False,
        git_deleted=False,
        error=f"the tree is held by session {holder}",
        error_code="worktree_not_eligible",
    )


def _refuse(
    db: HubDatabase, binding: TeamBinding, result: WorktreeDeletionResult
) -> dict[str, Any]:
    """File, or find again after a restart, the Triage finding for a refused deletion."""
    code = result.error_code or (
        "uncommitted_changes" if result.uncommitted_changes else "deletion_refused"
    )
    tasks = LocalTaskManager(db)
    label = f"team-{binding.team_id}"
    found = tasks.list_tasks(project_id=binding.project_id, label=label, closed=False, limit=1)
    finding = (
        found[0]
        if found
        else tasks.create_task(
            binding.project_id,
            f"Team teardown refused: {binding.branch}",
            description=(
                f"Task {binding.task_id} closed, its team's seats ended, and deleting its tree "
                f"was refused ({code}): {result.error or result.message}.\n\n"
                f"Tree: {binding.worktree_path}\nBranch: {binding.branch}\n"
                f"Base: {binding.base_branch}\n\n"
                "The tree is kept. Resolve the refusal, then delete it with "
                f"merged_into={binding.base_branch}, or record why it stays."
            ),
            labels=[FINDING_LABEL, label],
            validation_criteria=(
                f"The tree at {binding.worktree_path} is deleted with "
                f"merged_into={binding.base_branch}, or this task records why it stays."
            ),
        )
    )
    with contextlib.suppress(TaskAlreadyEscalatedError):
        tasks.escalate_task(finding.id, f"Team {binding.team_id} teardown refused: {code}")
    return {"outcome": "refused", "code": code, "finding": finding.id}


def _record(db: HubDatabase, binding: TeamBinding, outcome: dict[str, Any]) -> None:
    executions = LocalPipelineExecutionManager(db, binding.project_id)
    step = _admission_step(executions, binding.team_id)
    if step is not None:
        _merge(executions, step, **{TEARDOWN_KEY: outcome})
