"""Collapse a closed task's team: end its seats, then delete its tree safely (plan 3.3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
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
from gobby.sessions.handoff_records import build_handoff_payload, stage_agent_end_handoff
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun, LocalAgentRunManager
from gobby.storage.daemon_resume_keys import CONSUMED_AT_KEY, CONSUMED_BY_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.storage.tasks import LocalTaskManager, TaskAlreadyEscalatedError, TaskNotFoundError
from gobby.storage.team_records import ADMISSION_STEP
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline_state import StepExecution
from gobby.worktrees.containment import path_is_within
from gobby.worktrees.deletion import WorktreeDeletionResult

logger = logging.getLogger(__name__)

TEARDOWN_KEY = "teardown"
# The closing manager receives its close response before any seat ends.
TEARDOWN_GRACE = timedelta(seconds=30)
FINDING_LABEL = "team-teardown"
# The refusal code for a seat whose run cannot be ended; teardown reports it and keeps the tree.
SEAT_NOT_ENDED = "seat_not_ended"


class TeamSeatNotEndedError(RuntimeError):
    """A team seat's run could not be ended; teardown records a refusal, not a retry."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


TeamRunTerminator = Callable[[AgentRun], Awaitable[bool]]
TeamTreeDeleter = Callable[[TeamBinding], Awaitable[WorktreeDeletionResult]]


@dataclass(frozen=True)
class _DueTeam:
    binding: TeamBinding
    executions: list[str]  # the team's own execution and every execution that joined it


@dataclass(frozen=True)
class _DueLeaf:
    team: _DueTeam  # the epic team whose branch the leaf's sub-tree was cut from
    binding: TeamBinding  # the sub-tree, bound as a tree so the team's deleter removes it


@dataclass(frozen=True)
class _DueRunbook:
    execution_id: str
    project_id: str
    task_id: str
    seats: list[AgentRun]


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
    for leaf in await asyncio.to_thread(_due_leaves, db, cutoff):
        try:
            actions += await _tear_down_leaf(db, leaf, terminate, delete)
        except Exception:
            logger.exception("Leaf %s teardown failed; the next pass retries", leaf.binding.task_id)
    for runbook in await asyncio.to_thread(_due_runbook_seats, db, cutoff):
        try:
            ended, refusal = await _end_seats(db, terminate, runbook.seats)
            actions += sum(ended)
            if refusal is None and all(ended):
                await asyncio.to_thread(_expire_runs, db, runbook.seats)
            elif refusal is not None:
                await asyncio.to_thread(_refuse_runbook, db, runbook, refusal)
        except Exception:
            logger.exception("Runbook seat teardown failed; the next pass retries")
    return actions


def _due_runbook_seats(db: HubDatabase, cutoff: datetime) -> list[_DueRunbook]:
    """Task-bound runbooks without team admission still own the seats they launched.

    The saved definition and inputs are authoritative, including after a runbook finishes.
    Support has no task input. Restrict discovery to local executions with unfinished seats;
    admitted teams retain their existing tree cleanup and refusal handling.
    """
    rows = db.fetchall(
        """
SELECT DISTINCT p.id, p.project_id, p.session_id, p.inputs_json
FROM pipeline_executions p
JOIN sessions parent ON parent.id = p.session_id
JOIN agent_runs r ON r.parent_session_id = p.session_id
LEFT JOIN sessions child ON child.id = r.child_session_id
WHERE parent.machine_id = %s AND parent.source = 'pipeline'
AND p.definition_json -> 'tags' ? 'runbook'
AND p.inputs_json ->> 'task' IS NOT NULL
AND (r.status = ANY(%s) OR child.status = ANY(%s)
     OR (r.terminal_reason = 'daemon_stop'
         AND NOT (COALESCE(r.resume_metadata_json, '{}'::jsonb) ? %s)))
AND NOT EXISTS (
    SELECT 1 FROM step_executions se WHERE se.execution_id = p.id
    AND se.step_id = %s AND se.output_json #>> '{team,team_id}' IS NOT NULL
)
AND NOT EXISTS (
    SELECT 1 FROM step_executions se WHERE se.execution_id = p.id
    AND se.output_json #>> '{teardown,outcome}' IS NOT NULL
)
""",
        (
            require_machine_id(),
            list(ACTIVE_AGENT_RUN_STATUSES),
            list(LIVE_SESSION_STATUS_ORDER),
            CONSUMED_AT_KEY,
            ADMISSION_STEP,
        ),
    )
    tasks = LocalTaskManager(db)
    runs = LocalAgentRunManager(db)
    due: list[_DueRunbook] = []
    for row in rows:
        ref = json.loads(row["inputs_json"])["task"]
        try:
            task_id = tasks.resolve_task_reference(str(ref), str(row["project_id"]))
            task = tasks.get_task(task_id)
        except (TaskNotFoundError, ValueError):
            logger.warning("Runbook %s has an unresolved task %r", row["id"], ref)
            continue
        if task.closed_at is not None and task.closed_at <= cutoff:
            seats = runs.list_all_by_parent(str(row["session_id"]), limit=None)
            due.append(
                _DueRunbook(
                    str(row["id"]),
                    str(row["project_id"]),
                    task_id,
                    _eligible_seats(db, seats, task_id),
                )
            )
    return due


def _eligible_seats(db: HubDatabase, runs: list[AgentRun], task_id: str) -> list[AgentRun]:
    """A closed task cannot end a seat bound to, or still claiming, another open task."""
    if not runs:
        return []
    protected = {
        str(row["claimed_by_session_id"])
        for row in db.fetchall(
            "SELECT claimed_by_session_id FROM tasks WHERE closed_at IS NULL "
            "AND id <> %s AND claimed_by_session_id = ANY(%s)",
            (task_id, [run.child_session_id for run in runs if run.child_session_id]),
        )
    }
    tasks = LocalTaskManager(db)
    return [
        run
        for run in runs
        if run.child_session_id not in protected
        and (
            run.task_id is None
            or run.task_id == task_id
            or tasks.get_task(run.task_id).closed_at is not None
        )
    ]


def _protected_seats_remain(
    db: HubDatabase, seats: list[AgentRun], eligible: list[AgentRun]
) -> bool:
    """Keep teardown due while an excluded seat is running or may resume after a restart."""
    allowed = {run.id for run in eligible}
    protected = [run for run in seats if run.id not in allowed]
    if any(
        run.status in ACTIVE_AGENT_RUN_STATUSES
        or (
            run.terminal_reason == "daemon_stop"
            and CONSUMED_AT_KEY not in (run.resume_metadata_json or {})
        )
        for run in protected
    ):
        return True
    return bool(
        protected
        and db.fetchone(
            "SELECT 1 FROM sessions WHERE id = ANY(%s) AND status = ANY(%s) LIMIT 1",
            (
                [run.child_session_id for run in protected if run.child_session_id],
                list(LIVE_SESSION_STATUS_ORDER),
            ),
        )
    )


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
    return _with_members(db, due)


def _with_members(db: HubDatabase, bindings: list[TeamBinding]) -> list[_DueTeam]:
    """Pair each team with its own execution and every execution that joined it."""
    if not bindings:
        return []
    members: dict[str, list[str]] = {binding.team_id: [] for binding in bindings}
    for row in db.fetchall(
        "SELECT execution_id, output_json #>> '{team,team_id}' AS team_id FROM step_executions "
        "WHERE step_id = %s AND output_json #>> '{team,team_id}' = ANY(%s) ORDER BY id",
        (ADMISSION_STEP, list(members)),
    ):
        members[row["team_id"]].append(str(row["execution_id"]))
    return [_DueTeam(binding, members[binding.team_id]) for binding in bindings]


def _due_leaves(db: HubDatabase, cutoff: datetime) -> list[_DueLeaf]:
    """Sub-trees of epic leaves that closed before ``cutoff``, each paired with its epic's team.

    A sub-tree is cut from its team's branch, so an admission on this machine names that branch,
    and the team's task is an ancestor of the leaf at any depth. The team may still be open: a
    leaf can close while its epic works on.
    """
    rows = db.fetchall(
        """
SELECT w.id::text AS worktree_id, w.worktree_path, w.branch_name, w.base_branch,
       t.id::text AS task_id
FROM worktrees w
JOIN tasks t ON t.id = w.task_id
WHERE t.closed_at <= %s AND t.parent_task_id IS NOT NULL AND w.machine_id = %s
AND NOT EXISTS (
    SELECT 1 FROM step_executions se WHERE se.step_id = %s
    AND se.output_json #>> '{team,task_id}' = t.id::text
    AND se.output_json #>> '{team,parent_team_id}' IS NOT NULL
)
ORDER BY w.id
""",
        (cutoff, require_machine_id(), ADMISSION_STEP),
    )
    if not rows:
        return []
    ancestors = {row["task_id"]: _ancestor_ids(db, row["task_id"]) for row in rows}
    admissions = db.fetchall(
        "SELECT se.* FROM step_executions se "
        "WHERE se.step_id = %s AND se.output_json #>> '{team,team_id}' = se.execution_id::text "
        "AND se.output_json #>> '{team,task_id}' = ANY(%s)",
        (ADMISSION_STEP, sorted({task for tasks in ancestors.values() for task in tasks})),
    )
    bindings = [_binding(_recorded(StepExecution.from_row(row))) for row in admissions]
    teams = _with_members(db, [b for b in bindings if b])
    due: list[_DueLeaf] = []
    for row in rows:
        team = next(
            (
                team
                for team in teams
                if team.binding.task_id in ancestors[row["task_id"]]
                and team.binding.branch == row["base_branch"]
            ),
            None,
        )
        if team is None:
            continue
        binding = replace(
            team.binding,
            task_id=row["task_id"],
            worktree_id=row["worktree_id"],
            worktree_path=row["worktree_path"],
            branch=row["branch_name"],
            base_branch=row["base_branch"],
        )
        due.append(_DueLeaf(team, binding))
    return due


def _ancestor_ids(db: HubDatabase, task_id: str) -> set[str]:
    """Every ancestor of a task, at any depth."""
    rows = db.fetchall(
        """
WITH RECURSIVE up(id) AS (
    SELECT parent_task_id FROM tasks WHERE id = %s
    UNION
    SELECT t.parent_task_id FROM tasks t JOIN up ON t.id = up.id
)
SELECT id::text AS id FROM up WHERE id IS NOT NULL
""",
        (task_id,),
    )
    return {row["id"] for row in rows}


async def _tear_down(
    db: HubDatabase, team: _DueTeam, terminate: TeamRunTerminator, delete: TeamTreeDeleter
) -> int:
    folder = await asyncio.to_thread(release_epic_workspace, db, team.binding)
    seats = await asyncio.to_thread(_team_seats, db, team)
    dispatched = await asyncio.to_thread(
        _dispatched_runs, db, seats, team.binding.worktree_path, None
    )
    eligible = await asyncio.to_thread(
        _eligible_seats, db, [*seats, *dispatched], team.binding.task_id
    )
    ended, refusal = await _end_seats(db, terminate, eligible)
    if refusal is not None:
        label = f"team-{team.binding.team_id}"
        outcome = await asyncio.to_thread(_refuse, db, team.binding, refusal, label)
        await asyncio.to_thread(_record, db, team.binding, outcome)
        return sum(ended)
    if not all(ended):
        logger.warning("Team %s keeps live seats; the next pass retries", team.binding.team_id)
        return sum(ended)
    await asyncio.to_thread(_expire_seats, db, team)
    if await asyncio.to_thread(_protected_seats_remain, db, [*seats, *dispatched], eligible):
        logger.info("Team %s waits for seats on other tasks", team.binding.team_id)
        return sum(ended)
    if folder is not None:
        schedule_workspace_commit(team.binding.project_id, folder.name)
    if team.binding.worktree_id is None:  # a treeless team has no tree to delete
        await asyncio.to_thread(_record, db, team.binding, {"outcome": "cleaned"})
        return len(ended) + 1
    if await asyncio.to_thread(_sub_trees_remain, db, team):
        logger.info(
            "Team %s keeps its tree until its leaf sub-trees are gone", team.binding.team_id
        )
        return sum(ended)
    if await asyncio.to_thread(_occupied, db, dispatched):
        logger.info("Team %s keeps its tree while a dispatched seat exits", team.binding.team_id)
        return sum(ended)
    result = await _delete_tree(db, team.binding, delete)
    if result is None or result.abandoned:
        return len(ended)  # a shutdown or another cleanup interrupted it; the next pass retries
    outcome = (
        {"outcome": "cleaned"}
        if result.success
        else await asyncio.to_thread(
            _refuse, db, team.binding, result, f"team-{team.binding.team_id}"
        )
    )
    await asyncio.to_thread(_record, db, team.binding, outcome)
    return len(ended) + 1


async def _tear_down_leaf(
    db: HubDatabase, leaf: _DueLeaf, terminate: TeamRunTerminator, delete: TeamTreeDeleter
) -> int:
    """End the seats a merged leaf dispatched, then delete its sub-tree and branch.

    The epic is still open, so no teardown outcome goes on its admission. A retained
    sub-tree stays due, and its finding is found again on the next pass, not filed twice.
    """
    seats = await asyncio.to_thread(_team_seats, db, leaf.team)
    dispatched = await asyncio.to_thread(
        _dispatched_runs, db, seats, leaf.binding.worktree_path, leaf.binding.task_id
    )
    eligible = await asyncio.to_thread(_eligible_seats, db, dispatched, leaf.binding.task_id)
    ended, refusal = await _end_seats(db, terminate, eligible)
    label = f"leaf-{leaf.binding.worktree_id}"
    if refusal is not None:
        await asyncio.to_thread(_refuse, db, leaf.binding, refusal, label)
        return sum(ended)
    if not all(ended):
        return sum(ended)
    if await asyncio.to_thread(_protected_seats_remain, db, dispatched, eligible):
        return sum(ended)
    if await asyncio.to_thread(_occupied, db, dispatched):
        return sum(ended)
    result = await _delete_tree(db, leaf.binding, delete)
    if result is None or result.abandoned:
        return len(ended)
    if not result.success:
        await asyncio.to_thread(_refuse, db, leaf.binding, result, label)
        return len(ended)
    return len(ended) + 1


async def _end_seats(
    db: HubDatabase, terminate: TeamRunTerminator, runs: list[AgentRun]
) -> tuple[list[bool], WorktreeDeletionResult | None]:
    """Retire stopped seats even if a live seat refuses to end."""
    await asyncio.to_thread(
        _expire_runs, db, [run for run in runs if run.status not in ACTIVE_AGENT_RUN_STATUSES]
    )
    ended: list[bool] = []
    refusal: WorktreeDeletionResult | None = None
    for run in runs:
        if run.status not in ACTIVE_AGENT_RUN_STATUSES:
            continue
        try:
            await asyncio.to_thread(_stage_teardown_handoff, db, run)
            ended.append(await terminate(run))
        except TeamSeatNotEndedError as exc:
            if refusal is None:
                refusal = WorktreeDeletionResult(success=False, error_code=exc.code, error=str(exc))
    return ended, refusal


def _stage_teardown_handoff(db: HubDatabase, run: AgentRun) -> None:
    """Preserve any authored handoff; otherwise record why the daemon ended this seat."""
    if run.child_session_id is None:
        return
    stage_agent_end_handoff(
        db,
        agent_run_id=run.id,
        child_session_id=run.child_session_id,
        parent_session_id=run.parent_session_id,
        payload=build_handoff_payload(
            current_state="The runbook's task is closed. The daemon requested seat teardown.",
            next_steps=["No further work is assigned to this run."],
            references=[run.task_id] if run.task_id else [],
        ),
    )


async def _delete_tree(
    db: HubDatabase, binding: TeamBinding, delete: TeamTreeDeleter
) -> WorktreeDeletionResult | None:
    """Delete a tree; a lock that names a holder becomes a retry or a refusal."""
    result: WorktreeDeletionResult | None = await delete(binding)
    if result is not None and result.error_code == "worktree_not_eligible":
        return await asyncio.to_thread(_ineligible, db, binding)
    return result


def _team_seats(db: HubDatabase, team: _DueTeam) -> list[AgentRun]:
    """Every run the team's runbooks launched, ended or not.

    An ended seat still anchors its resumed run and the seats it dispatched. Resumes
    may be parented to the epic manager even when the original seat belongs to a pod.
    """
    executions = LocalPipelineExecutionManager(db, team.binding.project_id)
    runs = LocalAgentRunManager(db)
    seats: list[AgentRun] = []
    for execution_id in team.executions:
        execution = executions.get_execution(execution_id)
        if execution is not None and execution.session_id is not None:
            seats.extend(runs.list_all_by_parent(execution.session_id, limit=None))
    if seats:
        current = db.fetchall(
            "SELECT r.* FROM agent_runs r JOIN sessions s ON s.agent_run_id = r.id "
            "WHERE s.id = ANY(%s)",
            ([seat.child_session_id for seat in seats if seat.child_session_id],),
        )
        by_id = {seat.id: seat for seat in seats}
        by_id.update((run.id, run) for run in map(AgentRun.from_row, current))
        seats = list(by_id.values())
    return seats


def _sub_trees_remain(db: HubDatabase, team: _DueTeam) -> bool:
    """Whether a leaf sub-tree cut from the team's branch still exists on this machine.

    The team's tree is the merge target of those sub-trees, so it stays until they are gone.
    """
    row = db.fetchone(
        "SELECT 1 FROM worktrees "
        "WHERE project_id = %s AND base_branch = %s AND machine_id = %s LIMIT 1",
        (team.binding.project_id, team.binding.branch, require_machine_id()),
    )
    return row is not None


def _dispatched_runs(
    db: HubDatabase, seats: list[AgentRun], tree: str | None, task_id: str | None
) -> list[AgentRun]:
    """Runs a team seat dispatched for this task or into its tree, including parked seats.

    A dispatched seat is a child of the seat that dispatched it, not of the runbook. That seat
    may have ended already, so ``seats`` holds ended runs too, as ``_team_seats`` returns them.
    A leaf limits task bindings to its own task; None includes every task-bound child of
    a closed team so seats temporarily protected by another task remain discoverable.
    """
    if not seats:
        return []
    runs = LocalAgentRunManager(db)
    children = [
        run
        for seat in seats
        if seat.child_session_id is not None
        for run in runs.list_all_by_parent(seat.child_session_id, limit=None)
    ]
    if not children:
        return []
    paths = {
        row["id"]: row["workspace_path"]
        for row in db.fetchall(
            "SELECT id::text AS id, workspace_path FROM sessions WHERE id = ANY(%s)",
            ([run.child_session_id for run in children if run.child_session_id is not None],),
        )
    }
    inside = {
        session_id
        for session_id, path in paths.items()
        if tree is not None and path and path_is_within(path, tree)
    }
    return [
        run
        for run in children
        if (run.task_id is not None and (task_id is None or run.task_id == task_id))
        or str(run.child_session_id) in inside
    ]


def _occupied(db: HubDatabase, runs: list[AgentRun]) -> bool:
    """Whether any of these runs' sessions still works in a tree while its run is active."""
    if not runs:
        return False
    rows = db.fetchall(
        "SELECT 1 FROM agent_runs r JOIN sessions s ON s.id = r.child_session_id "
        "WHERE r.id = ANY(%s) AND r.status = ANY(%s) AND s.status = ANY(%s) LIMIT 1",
        (
            [run.id for run in runs],
            list(ACTIVE_AGENT_RUN_STATUSES),
            list(LIVE_SESSION_STATUS_ORDER),
        ),
    )
    return bool(rows)


def _expire_seats(db: HubDatabase, team: _DueTeam) -> None:
    _expire_runs(db, _eligible_seats(db, _team_seats(db, team), team.binding.task_id))


def _expire_runs(db: HubDatabase, runs: list[AgentRun]) -> None:
    """Expire every team seat's session a stop or cancel left live, and retire its resume.

    This covers runs already terminal before teardown (a daemon-stop parked seat),
    which no lookup of live seats ever sees. The parked resume is marked consumed by the
    teardown, so recovery skips it while other expired parked children stay resumable.
    """
    if not runs:
        return
    for run in runs:
        _stage_teardown_handoff(db, run)
    run_ids = [run.id for run in runs]
    retired = {CONSUMED_AT_KEY: datetime.now(UTC).isoformat(), CONSUMED_BY_KEY: TEARDOWN_KEY}
    db.execute(
        "UPDATE agent_runs SET resume_metadata_json = "
        "COALESCE(resume_metadata_json, '{}'::jsonb) || %s::jsonb "
        "WHERE id = ANY(%s) AND status = 'cancelled' "
        "AND terminal_reason = 'daemon_stop' "
        "AND NOT (COALESCE(resume_metadata_json, '{}'::jsonb) ? %s)",
        (json.dumps(retired), run_ids, CONSUMED_AT_KEY),
    )
    rows = db.fetchall(
        "SELECT DISTINCT s.id::text AS id FROM agent_runs r "
        "JOIN sessions s ON s.id = r.child_session_id "
        "WHERE r.id = ANY(%s) AND s.status = ANY(%s) "
        "AND NOT EXISTS (SELECT 1 FROM agent_runs live "
        "WHERE live.id = s.agent_run_id AND live.status = ANY(%s) "
        "AND NOT (live.id = ANY(%s)))",
        (run_ids, list(LIVE_SESSION_STATUS_ORDER), list(ACTIVE_AGENT_RUN_STATUSES), run_ids),
    )
    sessions = SessionManager(db)
    for row in rows:
        sessions.update_status(row["id"], "expired")


def _ineligible(db: HubDatabase, binding: TeamBinding) -> WorktreeDeletionResult | None:
    """Why the cleanup lock passed over the tree, or None when only a row lock held it."""
    tree = LocalWorktreeManager(db).get(binding.worktree_id) if binding.worktree_id else None
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


def _refuse_runbook(db: HubDatabase, runbook: _DueRunbook, result: WorktreeDeletionResult) -> None:
    """Persist a terminal refusal on the runbook and escalate its seats to Triage once."""
    executions = LocalPipelineExecutionManager(db, runbook.project_id)
    steps = executions.get_steps_for_execution(runbook.execution_id)
    if not steps:
        raise RuntimeError(f"Runbook {runbook.execution_id} launched seats without a recorded step")
    code = result.error_code or SEAT_NOT_ENDED
    label = f"runbook-{runbook.execution_id}"
    tasks = LocalTaskManager(db)
    found = tasks.list_tasks(project_id=runbook.project_id, label=label, closed=False, limit=1)
    seats = "\n".join(
        f"Run {run.id}; session {run.child_session_id}"
        for run in runbook.seats
        if run.status in ACTIVE_AGENT_RUN_STATUSES
    )
    finding = (
        found[0]
        if found
        else tasks.create_task(
            runbook.project_id,
            f"Runbook seat teardown refused: {runbook.execution_id}",
            description=(
                f"Task {runbook.task_id} closed, but runbook {runbook.execution_id} could not "
                f"end its seats ({code}): {result.error or result.message}.\n\n{seats}\n\n"
                "Automatic teardown is stopped for this runbook. Resolve the refusal and end "
                "these runs, preserving their handoffs, or record why a seat must remain."
            ),
            labels=[FINDING_LABEL, label],
            validation_criteria=(
                "The listed runs have ended with handoffs, or this task records why they remain."
            ),
        )
    )
    with contextlib.suppress(TaskAlreadyEscalatedError):
        tasks.escalate_task(finding.id, f"Runbook {runbook.execution_id} teardown refused: {code}")
    # The first launch/guard step is already complete before any seat can close the task.
    # Merge into its durable output without adding a step absent from the saved definition.
    _merge(
        executions,
        steps[0],
        teardown={"outcome": "refused", "code": code, "finding": finding.id},
    )


def _refuse(
    db: HubDatabase, binding: TeamBinding, result: WorktreeDeletionResult, label: str
) -> dict[str, Any]:
    """File, or find again after a restart, the Triage finding for a refused deletion.

    ``label`` names the tree's finding: a team tree's is its team's, and each retained leaf
    sub-tree of an epic keeps its own, so one leaf's finding never answers for another.
    """
    code = result.error_code or (
        "uncommitted_changes" if result.uncommitted_changes else "deletion_refused"
    )
    tasks = LocalTaskManager(db)
    found = tasks.list_tasks(project_id=binding.project_id, label=label, closed=False, limit=1)
    finding = (
        found[0]
        if found
        else tasks.create_task(
            binding.project_id,
            f"Team teardown refused: {binding.branch}",
            description=(
                f"Task {binding.task_id} closed and its team's teardown was refused ({code}): "
                f"{result.error or result.message}.\n\n"
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
