"""Collapse a closed task's team: end its seats and keep its tree for the completion owner."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.agents.agent_workspaces import release_epic_workspace
from gobby.agents.team_admission_records import (
    TeamBinding,
    admission_step,
    binding_from_record,
    merge_step_output,
    recorded_team,
)
from gobby.sessions.handoff_records import build_handoff_payload, stage_agent_end_handoff
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun, LocalAgentRunManager
from gobby.storage.daemon_resume_keys import CONSUMED_AT_KEY, CONSUMED_BY_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError
from gobby.storage.team_records import ADMISSION_STEP
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline_state import StepExecution
from gobby.worktrees.containment import path_is_within
from gobby.worktrees.deletion import WorktreeDeletionResult

logger = logging.getLogger(__name__)

TEARDOWN_KEY = "teardown"
# The closing manager receives its close response before any seat ends.
TEARDOWN_GRACE = timedelta(seconds=30)
# The refusal code for a seat whose run cannot be ended; teardown reports it and keeps the tree.
SEAT_NOT_ENDED = "seat_not_ended"
# Last (code, reason) logged per tree this process. A refused team is retried every pass, so a
# refusal logs at WARNING once and again only when its code or reason changes; later passes log at
# DEBUG. In-process memory only: nothing here files a task or writes database state.
_last_refusals: dict[tuple[str, str | None], tuple[str, str]] = {}


class TeamSeatNotEndedError(RuntimeError):
    """A team seat's run could not be ended; teardown records a refusal, not a retry."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


TeamRunTerminator = Callable[[AgentRun, str | None], Awaitable[bool]]


@dataclass(frozen=True)
class _DueTeam:
    binding: TeamBinding
    executions: list[str]  # the team's own execution and every execution that joined it


@dataclass(frozen=True)
class _DueLeaf:
    team: _DueTeam  # the team of the leaf's parent task, whose branch the sub-tree was cut from
    binding: TeamBinding  # the sub-tree, bound as a tree; teardown ends its seats and keeps it


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
    now: datetime | None = None,
    grace: timedelta = TEARDOWN_GRACE,
) -> int:
    """One stateless pass over every closed task's team; returns the actions taken."""
    cutoff = (now or datetime.now(UTC)) - grace
    actions = 0
    for team in await asyncio.to_thread(_due_teams, db, cutoff):
        try:
            actions += await _tear_down(db, team, terminate)
        except Exception:
            logger.exception("Team %s teardown failed; the next pass retries", team.binding.team_id)
    for leaf in await asyncio.to_thread(_due_leaves, db, cutoff):
        try:
            actions += await _tear_down_leaf(db, leaf, terminate)
        except Exception:
            logger.exception("Leaf %s teardown failed; the next pass retries", leaf.binding.task_id)
    for runbook in await asyncio.to_thread(_due_runbook_seats, db, cutoff):
        try:
            ended, refusal = await _end_seats(db, terminate, runbook.seats, runbook.task_id)
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
    Executions without a task input are not discovered. Restrict discovery to local executions
    with unfinished seats; admitted teams belong to _due_teams, which keeps their trees.
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


def _unfinished_runs(db: HubDatabase, runs: list[AgentRun]) -> list[AgentRun]:
    """Runs a stop still owes: active, parked by a daemon stop and unconsumed, or session live."""
    if not runs:
        return []
    live = {
        str(row["id"])
        for row in db.fetchall(
            "SELECT id::text AS id FROM sessions WHERE id = ANY(%s) AND status = ANY(%s)",
            (
                [run.child_session_id for run in runs if run.child_session_id],
                list(LIVE_SESSION_STATUS_ORDER),
            ),
        )
    }
    return [
        run
        for run in runs
        if run.status in ACTIVE_AGENT_RUN_STATUSES
        or (
            run.terminal_reason == "daemon_stop"
            and CONSUMED_AT_KEY not in (run.resume_metadata_json or {})
        )
        or (run.child_session_id is not None and str(run.child_session_id) in live)
    ]


def _protected_seats_remain(
    db: HubDatabase, seats: list[AgentRun], eligible: list[AgentRun]
) -> bool:
    """Keep teardown due while an excluded seat is running or may resume after a restart."""
    allowed = {run.id for run in eligible}
    return bool(_unfinished_runs(db, [run for run in seats if run.id not in allowed]))


def _due_teams(db: HubDatabase, cutoff: datetime) -> list[_DueTeam]:
    """Teams whose task closed before ``cutoff`` and whose own record has no outcome yet.

    Only the daemon on the tree's machine can end its seats, so a tree recorded on another
    machine is left to that daemon.
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
    due = [
        b
        for b in (binding_from_record(recorded_team(StepExecution.from_row(row))) for row in rows)
        if b
    ]
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
    """Sub-trees of leaves that closed before ``cutoff``, each paired with its parent task's team.

    A sub-tree is cut from its team's branch, so an admission on this machine names that branch,
    and the team's task is an ancestor of the leaf at any depth. The team may still be open: a
    leaf can close while its parent task works on.
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
    bindings = [
        binding_from_record(recorded_team(StepExecution.from_row(row))) for row in admissions
    ]
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


async def _tear_down(db: HubDatabase, team: _DueTeam, terminate: TeamRunTerminator) -> int:
    await asyncio.to_thread(release_epic_workspace, db, team.binding)
    seats = await asyncio.to_thread(_team_seats, db, team)
    dispatched = await asyncio.to_thread(
        _dispatched_runs, db, seats, team.binding.worktree_path, None
    )
    eligible = await asyncio.to_thread(
        _eligible_seats, db, [*seats, *dispatched], team.binding.task_id
    )
    ended, refusal = await _end_seats(db, terminate, eligible, team.binding.task_id)
    if refusal is not None:
        outcome = _refuse(team.binding, refusal)
        await asyncio.to_thread(_record, db, team.binding, outcome)
        return sum(ended)
    if not all(ended):
        logger.warning("Team %s keeps live seats; the next pass retries", team.binding.team_id)
        return sum(ended)
    await asyncio.to_thread(_expire_seats, db, team)
    if await asyncio.to_thread(_protected_seats_remain, db, [*seats, *dispatched], eligible):
        logger.info("Team %s waits for seats on other tasks", team.binding.team_id)
        return sum(ended)
    if team.binding.worktree_id is None:  # a treeless team has no tree to keep
        await asyncio.to_thread(_record, db, team.binding, {"outcome": "cleaned"})
        return len(ended) + 1
    if await asyncio.to_thread(_occupied, db, dispatched):
        logger.info("Team %s waits while a dispatched seat exits", team.binding.team_id)
        return sum(ended)
    await asyncio.to_thread(_record, db, team.binding, {"outcome": "retained"})
    return len(ended) + 1


async def _tear_down_leaf(db: HubDatabase, leaf: _DueLeaf, terminate: TeamRunTerminator) -> int:
    """End the unfinished seats a merged leaf dispatched; its tree and branch stay.

    The completion owner's delete_worktree retires the tree. The parent task is still open,
    so no teardown outcome goes on its admission. With no unfinished seat left, a pass is a
    read that writes nothing.
    """
    seats = await asyncio.to_thread(_team_seats, db, leaf.team)
    dispatched = await asyncio.to_thread(
        _dispatched_runs, db, seats, leaf.binding.worktree_path, leaf.binding.task_id
    )
    eligible = await asyncio.to_thread(_eligible_seats, db, dispatched, leaf.binding.task_id)
    pending = await asyncio.to_thread(_unfinished_runs, db, eligible)
    if not pending:
        return 0
    ended, refusal = await _end_seats(db, terminate, pending, leaf.binding.task_id)
    if refusal is not None:
        _refuse(leaf.binding, refusal)
    return sum(ended)


async def _end_seats(
    db: HubDatabase, terminate: TeamRunTerminator, runs: list[AgentRun], task_id: str
) -> tuple[list[bool], WorktreeDeletionResult | None]:
    """Retire stopped seats even if a live seat refuses to end."""
    task = await asyncio.to_thread(LocalTaskManager(db).get_task, task_id)
    closed_reason = task.closed_reason
    await asyncio.to_thread(
        _expire_runs, db, [run for run in runs if run.status not in ACTIVE_AGENT_RUN_STATUSES]
    )
    ended: list[bool] = []
    refusal: WorktreeDeletionResult | None = None
    for run in runs:
        if run.status not in ACTIVE_AGENT_RUN_STATUSES:
            continue
        try:
            await asyncio.to_thread(_stage_teardown_handoff, db, run, closed_reason)
            ended.append(await terminate(run, closed_reason))
        except TeamSeatNotEndedError as exc:
            if refusal is None:
                refusal = WorktreeDeletionResult(success=False, error_code=exc.code, error=str(exc))
    return ended, refusal


def _stage_teardown_handoff(
    db: HubDatabase, run: AgentRun, closed_reason: str | None = None
) -> None:
    """Preserve any authored handoff; otherwise record why the daemon ended this seat."""
    if run.child_session_id is None:
        return
    stage_agent_end_handoff(
        db,
        agent_run_id=run.id,
        child_session_id=run.child_session_id,
        parent_session_id=run.parent_session_id,
        payload=build_handoff_payload(
            current_state=(
                f"The runbook's task is closed ({closed_reason or 'unspecified'}). "
                "The daemon requested seat teardown."
            ),
            next_steps=["No further work is assigned to this run."],
            references=[run.task_id] if run.task_id else [],
        ),
    )


def _team_seats(db: HubDatabase, team: _DueTeam) -> list[AgentRun]:
    """Every run the team's runbooks launched, ended or not.

    An ended seat still anchors its resumed run and the seats it dispatched. Resumes
    may be parented to the parent task's manager even when the original seat belongs to a pod.
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
    """Whether any of these runs is active and, if attached, its session is live."""
    if not runs:
        return False
    rows = db.fetchall(
        "SELECT 1 FROM agent_runs r LEFT JOIN sessions s ON s.id = r.child_session_id "
        "WHERE r.id = ANY(%s) AND r.status = ANY(%s) AND (s.id IS NULL OR s.status = ANY(%s)) "
        "LIMIT 1",
        (
            [run.id for run in runs],
            list(ACTIVE_AGENT_RUN_STATUSES),
            list(LIVE_SESSION_STATUS_ORDER),
        ),
    )
    return bool(rows)


def team_seats_live(db: HubDatabase, binding: TeamBinding) -> bool:
    """Whether a team's seats, or the seats they dispatched, still work in its tree."""
    (team,) = _with_members(db, [binding])
    seats = _team_seats(db, team)
    return _occupied(db, [*seats, *_dispatched_runs(db, seats, binding.worktree_path, None)])


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


def _refuse_runbook(db: HubDatabase, runbook: _DueRunbook, result: WorktreeDeletionResult) -> None:
    """Persist a terminal refusal on the runbook and log it; no task is filed."""
    executions = LocalPipelineExecutionManager(db, runbook.project_id)
    steps = executions.get_steps_for_execution(runbook.execution_id)
    if not steps:
        raise RuntimeError(f"Runbook {runbook.execution_id} launched seats without a recorded step")
    code = result.error_code or SEAT_NOT_ENDED
    reason = result.error or result.message or code
    run_ids = [run.id for run in runbook.seats if run.status in ACTIVE_AGENT_RUN_STATUSES]
    logger.warning(
        "Runbook %s seat teardown refused (%s): %s",
        runbook.execution_id,
        code,
        reason,
        extra={
            "runbook_id": runbook.execution_id,
            "task_id": runbook.task_id,
            "code": code,
            "reason": reason,
            "run_ids": run_ids,
        },
    )
    # The first launch/guard step is already complete before any seat can close the task.
    # Merge into its durable output without adding a step absent from the saved definition.
    merge_step_output(executions, steps[0], teardown={"outcome": "refused", "code": code})


def _refuse(binding: TeamBinding, result: WorktreeDeletionResult) -> dict[str, Any]:
    """Log a seat that refused to end and return its refused outcome; no task is filed."""
    code = result.error_code or (
        "uncommitted_changes" if result.uncommitted_changes else "deletion_refused"
    )
    reason = result.error or result.message or code
    key = (binding.team_id, binding.worktree_id)
    repeated = _last_refusals.get(key) == (code, reason)
    _last_refusals[key] = (code, reason)
    logger.log(
        logging.DEBUG if repeated else logging.WARNING,
        "Team %s teardown refused for tree %s (%s): %s",
        binding.team_id,
        binding.worktree_path,
        code,
        reason,
        extra={
            "team_id": binding.team_id,
            "task_id": binding.task_id,
            "worktree_id": binding.worktree_id,
            "worktree_path": binding.worktree_path,
            "branch": binding.branch,
            "code": code,
            "reason": reason,
        },
    )
    return {"outcome": "refused", "code": code}


def _record(db: HubDatabase, binding: TeamBinding, outcome: dict[str, Any]) -> None:
    executions = LocalPipelineExecutionManager(db, binding.project_id)
    step = admission_step(executions, binding.team_id)
    if step is not None:
        merge_step_output(executions, step, **{TEARDOWN_KEY: outcome})
