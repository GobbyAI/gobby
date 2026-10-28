"""Single-use teams: one task, one branch and worktree, bound to the execution that admits it.

The team record is the admitting runbook execution: its ``admit`` step output carries the
binding, and ``team_id`` is that execution's id. Admission runs under
``MainCheckoutLanding`` so team admission and landing serialize. It
records the team before it creates the tree, so a re-run of a step that died mid-launch
adopts its own tree. A retry of the admitting execution returns the same team; a
replacement execution names the team it joins with ``team_id`` and must match its task.
A fresh-reviewer execution joins the same way after the second review rejection; the team's
``admit`` output keeps its id under ``fresh_reviewers`` per review epoch, so that epoch admits
no other. Seat spawns from a team runbook join the admitted tree and cannot create a branch,
worktree or clone or carry another task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from gobby.agents.isolation_models import SpawnConfig, generate_branch_name
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.agents.team_launch_inputs import reviewer_refusal
from gobby.agents.team_scope import (
    SINGLE_MODE,
    ScopeRefused,
    admission_mode,
    epic_names,
    epic_team_above,
    live_teams,
    open_leaf_below,
    tree_is_live,
)
from gobby.code_index.gcode_gateway import GcodeGateway, GcodeGatewayError
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase, MainCheckoutLanding
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.session_resolution import resolve_session_reference
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.storage.tasks._de_escalation import REVIEW_REJECTION_LIMIT
from gobby.storage.team_records import ADMISSION_STEP, EPIC_MODE
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.tasks.land_commit import LandingGitError, main_checkout_target
from gobby.tasks.step_records import list_task_step_records
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.uuid_validation import parse_uuid_reference
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.pipeline_state import ExecutionStatus, StepExecution
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)
_PIPELINE_SOURCE = "pipeline"
_PIPELINE_PREFIX = "pipeline-"
_SEAT_DEPTH_LIMIT = 5
_TREE_FIELDS = frozenset({"worktree_id", "worktree_path"})
_WORKTREE = "worktree"
_TREELESS = "none"


@dataclass(frozen=True)
class TeamBinding:
    """One admitted team: its task, branch and worktree, and the admitting execution."""

    team_id: str
    task_id: str
    project_id: str
    branch: str
    base_branch: str
    # None for a treeless team, whose admission records an explicit null for its tree.
    worktree_id: str | None
    worktree_path: str | None
    # One entry per reviewer definition the team binds; None until its seat exists.
    reviewers: dict[str, str | None] = field(default_factory=dict)
    # One entry per implementation domain: the live primary that works the team tree.
    primaries: dict[str, str | None] = field(default_factory=dict)
    # "epic" for a team bound to an epic's open leaves; "single" for one task.
    mode: str = SINGLE_MODE
    parent_team_id: str | None = None


@dataclass(frozen=True)
class SeatTarget:
    """Where a pipeline seat spawn asks to run, as spawn_agent received it."""

    task_id: str | None = None
    checkout_mode: str | None = None
    branch_name: str | None = None
    base_branch: str | None = None
    clone_id: str | None = None
    worktree_id: str | None = None
    project_path: str | None = None


class TeamAdmissionRefused(Exception):
    """Team admission or a team seat spawn is refused; ``code`` names the cause."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Caller:
    """A running team runbook execution and its admission step row."""

    session: Session
    project_id: str
    executions: LocalPipelineExecutionManager
    execution_id: str
    step: StepExecution


async def admit_team(
    db: HubDatabase,
    *,
    caller_session_id: str,
    task: str,
    team_id: str | None = None,
    parent_team_id: str | None = None,
    fresh_reviewer: bool = False,
    exclude_reviewers: Sequence[str] = (),
    reviewer_agent: str | None = None,
    checkout_mode: str = _WORKTREE,
) -> TeamBinding:
    """Admit the caller execution's team for ``task``, or join team ``team_id``.

    A replacement execution joins with ``team_id``. A ``fresh_reviewer`` execution joins only
    after the second rejection handed the claim to a reviewer, as the epoch's one fresh reviewer.
    ``checkout_mode="none"`` admits a treeless team: no branch or tree, seats in the main
    checkout. A treeless admission for a task joins the task's live treeless team.
    """
    if fresh_reviewer and team_id is None:
        raise TeamAdmissionRefused(
            "fresh_reviewer_team_required", "a fresh reviewer joins an admitted team: pass team_id"
        )
    if checkout_mode not in (_WORKTREE, _TREELESS):
        raise TeamAdmissionRefused(
            "checkout_mode_invalid", f"checkout_mode {checkout_mode} is not worktree or none"
        )
    if team_id is not None:
        team_uuid = parse_uuid_reference(team_id)
        if team_uuid is None:
            raise TeamAdmissionRefused(
                "team_missing", f"team {team_id} requires a full execution UUID for team_id"
            )
        team_id = str(team_uuid)
    caller = await asyncio.to_thread(_team_caller, db, caller_session_id)
    target = await asyncio.to_thread(_open_task, db, task, caller.project_id)
    if parent_team_id is not None:
        if team_id is not None or fresh_reviewer or checkout_mode != _WORKTREE:
            raise TeamAdmissionRefused("pod_options_invalid", "a pod creates its own worktree team")
    if reviewer_agent is not None:
        refusal = await asyncio.to_thread(reviewer_refusal, db, caller.project_id, reviewer_agent)
        if refusal is not None:
            raise TeamAdmissionRefused("reviewer_refused", refusal)
    async with db.advisory_lock(MainCheckoutLanding(caller.project_id)):
        own = await asyncio.to_thread(_own_record, caller)
        if own is not None:
            if own.get("parent_team_id") != parent_team_id:
                if team_id is None:
                    raise TeamAdmissionRefused(
                        "team_binding_drift", "the team's parent cannot change"
                    )
            return await _readmit(db, caller, own, target, team_id, reviewer_agent)
        parent = (
            await asyncio.to_thread(
                pod_parent, db, caller.session.parent_session_id or "", target.id, parent_team_id
            )
            if parent_team_id
            else None
        )
        if team_id is None and checkout_mode == _TREELESS:
            # A treeless admission for a task joins the task's live treeless team.
            team_id = await asyncio.to_thread(_live_treeless_team, db, caller.project_id, target.id)
        if team_id is None:
            # Only a team with no record is new; recorded and joined teams keep working.
            if checkout_mode == _TREELESS:
                return await _create_treeless(db, caller, target, reviewer_agent)
            return await _create(db, caller, target, reviewer_agent, parent)
        if fresh_reviewer:
            await asyncio.to_thread(
                _reserve_fresh_reviewer, db, caller, target, team_id, exclude_reviewers
            )
        return await _join(db, caller, target, team_id, reviewer_agent)


def check_team_spawn(db: HubDatabase, caller: Session, target: SeatTarget) -> None:
    """Refuse a seat spawn by a team runbook that would leave its admitted team."""
    binding = _caller_binding(db, caller)
    if binding is None:
        return
    if LocalTaskManager(db).get_task(binding.task_id).closed_at is not None:
        raise TeamAdmissionRefused(
            "team_closed", f"team {binding.team_id} collapsed when task {binding.task_id} closed"
        )
    if (
        target.checkout_mode in ("worktree", "clone")
        or target.branch_name
        or target.base_branch
        or target.clone_id
    ):
        raise TeamAdmissionRefused(
            "team_tree_fixed",
            "team seats join the admitted tree; they cannot create a branch, worktree or clone",
        )
    if target.task_id is not None and _task_id(db, target.task_id, binding) != binding.task_id:
        raise TeamAdmissionRefused(
            "team_task_mismatch", f"team {binding.team_id} is bound to task {binding.task_id}"
        )
    if target.worktree_id is None and target.project_path is None:
        raise TeamAdmissionRefused(
            "team_tree_required",
            f"team seats name the team tree {binding.worktree_id} by worktree_id or project_path",
        )
    if target.worktree_id is not None and _tree_id(db, target.worktree_id) != binding.worktree_id:
        raise TeamAdmissionRefused(
            "team_tree_mismatch", f"team {binding.team_id} works in tree {binding.worktree_id}"
        )
    if target.project_path is not None and (
        binding.worktree_path is None
        or Path(target.project_path).resolve() != Path(binding.worktree_path).resolve()
    ):
        raise TeamAdmissionRefused(
            "team_tree_mismatch", f"team {binding.team_id} works in {binding.worktree_path}"
        )


def _team_caller(db: HubDatabase, caller_session_id: str) -> _Caller:
    session = SessionManager(db).get(caller_session_id)
    if (
        session is None
        or session.source != _PIPELINE_SOURCE
        or not session.external_id.startswith(_PIPELINE_PREFIX)
        or session.project_id is None
    ):
        raise TeamAdmissionRefused(
            "team_caller_invalid", f"caller session {caller_session_id} is not a pipeline child"
        )
    executions = LocalPipelineExecutionManager(db, session.project_id)
    execution_id = session.external_id.removeprefix(_PIPELINE_PREFIX)
    execution = executions.get_execution(execution_id)
    if execution is None or execution.status != ExecutionStatus.RUNNING:
        raise TeamAdmissionRefused(
            "team_caller_invalid", f"pipeline execution {execution_id} is not running"
        )
    step = _admission_step(executions, execution_id)
    if step is None:
        raise TeamAdmissionRefused(
            "team_caller_invalid",
            f"execution {execution_id} admits a team only from its '{ADMISSION_STEP}' step",
        )
    return _Caller(session, session.project_id, executions, execution_id, step)


def _open_task(db: HubDatabase, ref: str, project_id: str) -> Task:
    tasks = LocalTaskManager(db)
    try:
        task = tasks.get_task(tasks.resolve_task_reference(ref, project_id), project_id)
    except (TaskNotFoundError, ValueError) as exc:
        raise TeamAdmissionRefused("team_task_unknown", str(exc)) from exc
    if task.closed_at is not None:
        raise TeamAdmissionRefused("team_task_closed", f"task {task.id} is closed")
    if task.seq_num is None:
        raise TeamAdmissionRefused("team_task_unknown", f"task {task.id} has no sequence number")
    return task


def _admission_step(
    executions: LocalPipelineExecutionManager, execution_id: str
) -> StepExecution | None:
    steps = executions.get_steps_for_execution(execution_id)
    return next((step for step in steps if step.step_id == ADMISSION_STEP), None)


def _output(step: StepExecution | None) -> dict[str, Any]:
    try:
        output = json.loads(step.output_json or "null") if step is not None else None
    except json.JSONDecodeError:
        return {}
    return output if isinstance(output, dict) else {}


def _merge(executions: LocalPipelineExecutionManager, step: StepExecution, **keys: Any) -> None:
    """Set ``keys`` in an admission step's output and keep its other keys."""
    output = {**_output(step), **keys}
    executions.update_step_execution(step.id, output_json=json.dumps(output))


def _recorded(step: StepExecution | None) -> dict[str, Any] | None:
    """The team record in an admission step's output, complete or not."""
    record = _output(step).get("team")
    return record if isinstance(record, dict) else None


def _binding(record: dict[str, Any] | None) -> TeamBinding | None:
    if record is None:
        return None
    values: dict[str, Any] = {}
    for item in fields(TeamBinding):
        value = record.get(item.name)
        if item.name in ("reviewers", "primaries"):
            # The admission intent carries neither map until a seat binds.
            values[item.name] = dict(value) if isinstance(value, dict) else {}
            continue
        if item.name == "mode":
            # Records admitted before epic mode carry no mode: they are single-task teams.
            values[item.name] = value if isinstance(value, str) and value else SINGLE_MODE
            continue
        if item.name == "parent_team_id" and value is None:
            values[item.name] = None
            continue
        if item.name in ("worktree_id", "worktree_path") and item.name in record and value is None:
            values[item.name] = None
            continue
        if not isinstance(value, str) or not value:
            return None
        values[item.name] = value
    return TeamBinding(**values)


def _bound_reviewer(reviewers: Any, reviewer_agent: str | None, team_id: str) -> None:
    """A team binds its reviewer definitions; a join must name one the team already binds."""
    if reviewer_agent is None or not isinstance(reviewers, dict) or not reviewers:
        return
    if reviewer_agent not in reviewers:
        bound = ", ".join(sorted(reviewers))
        raise TeamAdmissionRefused(
            "reviewer_mismatch", f"{reviewer_agent} cannot join team {team_id} bound to {bound}"
        )


def _own_record(caller: _Caller) -> dict[str, Any] | None:
    return _recorded(_admission_step(caller.executions, caller.execution_id))


def _record(caller: _Caller, record: dict[str, Any]) -> None:
    step = _admission_step(caller.executions, caller.execution_id) or caller.step
    _merge(caller.executions, step, team=record)


async def _readmit(
    db: HubDatabase,
    caller: _Caller,
    own: dict[str, Any],
    task: Task,
    team_id: str | None,
    reviewer_agent: str | None,
) -> TeamBinding:
    """The caller already recorded a team: return it, adopting a tree made before a crash."""
    named = team_id or caller.execution_id
    if own.get("checkout_mode") == _TREELESS and team_id is None:
        # A treeless team is named by its task, so a joined execution re-admits the team it joined.
        named = str(own.get("team_id"))
    admitted_task = own.get("admitted_task_id", own.get("task_id"))
    if admitted_task != task.id or own.get("team_id") != named:
        raise TeamAdmissionRefused(
            "team_binding_drift",
            f"execution {caller.execution_id} is bound to team {own.get('team_id')} "
            f"for task {own.get('task_id')}",
        )
    _bound_reviewer(own.get("reviewers"), reviewer_agent, str(own.get("team_id")))
    if own.get("task_id") != task.id:
        _team(caller, task, named)
    binding = _binding(own)
    if binding is not None:
        if own.get("checkout_mode") != _TREELESS:
            await _verify_tree(db, binding)
        return binding
    if own.get("checkout_mode") == _TREELESS:
        # A treeless record is written whole; an incomplete one must not fall through to a tree.
        raise TeamAdmissionRefused(
            "team_binding_drift", f"treeless team {own.get('team_id')} has an incomplete record"
        )
    branch = own.get("branch")
    tree = await asyncio.to_thread(
        LocalWorktreeManager(db).get_by_branch, caller.project_id, str(branch)
    )
    if tree is None or not Path(tree.worktree_path).is_dir():
        parent = team_binding(db, str(own["parent_team_id"])) if own.get("parent_team_id") else None
        return await _create(db, caller, task, reviewer_agent, parent)
    if tree.task_id != task.id:
        raise TeamAdmissionRefused(
            "team_branch_taken", f"branch {branch} belongs to tree {tree.id} of another task"
        )
    binding = TeamBinding(
        **{key: own[key] for key in own if key not in _TREE_FIELDS | {"admitted_task_id"}},
        worktree_id=tree.id,
        worktree_path=tree.worktree_path,
    )
    await _verify_tree(db, binding)
    await asyncio.to_thread(_record, caller, asdict(binding))
    return binding


def _team(caller: _Caller, task: Task, team_id: str) -> tuple[StepExecution, TeamBinding]:
    """Team ``team_id``'s admission step and its complete binding for ``task``."""
    team = caller.executions.get_execution(team_id)
    step = _admission_step(caller.executions, team_id) if team else None
    binding = _binding(_recorded(step))
    if step is None or binding is None or binding.project_id != caller.project_id:
        raise TeamAdmissionRefused("team_missing", f"team {team_id} has no complete admission")
    if binding.task_id != task.id and not (
        binding.mode == EPIC_MODE
        and open_leaf_below(caller.executions.db, binding.task_id, task.id) is not None
        and task.id not in live_teams(caller.executions.db, [task.id])
    ):
        raise TeamAdmissionRefused(
            "team_task_mismatch", f"team {team_id} is bound to task {binding.task_id}"
        )
    return step, binding


async def _join(
    db: HubDatabase, caller: _Caller, task: Task, team_id: str, reviewer_agent: str | None
) -> TeamBinding:
    """A replacement or fresh-reviewer execution joins team ``team_id`` for the same task."""
    step, binding = await asyncio.to_thread(_team, caller, task, team_id)
    _bound_reviewer(binding.reviewers, reviewer_agent, team_id)
    # A treeless team has no tree to verify; its joined executions record the same marker.
    treeless = (_recorded(step) or {}).get("checkout_mode") == _TREELESS
    if not treeless:
        await _verify_tree(db, binding)
    record = {**asdict(binding), "checkout_mode": _TREELESS} if treeless else asdict(binding)
    if binding.task_id != task.id:
        record["admitted_task_id"] = task.id
    await asyncio.to_thread(_record, caller, record)
    return binding


_ENDED_EXECUTIONS = frozenset(
    {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)


def _holds_epoch(db: HubDatabase, executions: LocalPipelineExecutionManager, holder: str) -> bool:
    """Whether a reserved fresh-reviewer execution still holds its epoch's slot.

    An execution still in progress holds it, since its reviewer may not have spawned yet. An
    ended execution holds it only while its code-reviewer seat runs. A failed pipeline or a
    refused spawn leaves no active seat, so it frees the slot for a relaunch.
    """
    execution = executions.get_execution(holder)
    if execution is None or execution.status not in _ENDED_EXECUTIONS:
        return True
    reviewer = LocalAgentRunManager(db).get(step_invocation_id(holder, "code-reviewer"))
    return reviewer is not None and reviewer.status in ACTIVE_AGENT_RUN_STATUSES


def _reserve_fresh_reviewer(
    db: HubDatabase, caller: _Caller, task: Task, team_id: str, exclude_reviewers: Sequence[str]
) -> None:
    """Record the caller as the epoch's fresh reviewer once rejection two moved the claim."""
    step, binding = _team(caller, task, team_id)
    current = LocalTaskManager(db).get_task(task.id, caller.project_id)
    epoch = current.review_epoch
    reviews = [
        record
        for record in list_task_step_records(db, task.id)
        if record.step == "review" and record.epoch == epoch
    ]
    rejected = {record.round_number for record in reviews if record.status == "rejected"}
    if len(rejected) != REVIEW_REJECTION_LIMIT - 1:
        raise TeamAdmissionRefused(
            "fresh_reviewer_round",
            f"task {task.id} has {len(rejected)} rejected review rounds in epoch {epoch}; "
            f"a fresh reviewer starts at {REVIEW_REJECTION_LIMIT - 1}",
        )
    reviewers = {record.session_id for record in reviews}
    try:
        excluded = {
            resolve_session_reference(db, ref, caller.project_id) for ref in exclude_reviewers
        }
    except ValueError as exc:
        raise TeamAdmissionRefused("fresh_reviewer_exclusions", str(exc)) from exc
    if missing := reviewers - excluded:
        raise TeamAdmissionRefused(
            "fresh_reviewer_exclusions",
            f"exclude_reviewers omits epoch {epoch} reviewers {', '.join(sorted(missing))}",
        )
    if current.claimed_by_session_id not in reviewers:
        raise TeamAdmissionRefused(
            "fresh_reviewer_claim", f"task {task.id}'s claim has not passed to a reviewer"
        )
    reserved = _output(step).get("fresh_reviewers")
    reserved = reserved if isinstance(reserved, dict) else {}
    reservation = f"{task.id}:{epoch}" if binding.mode == EPIC_MODE else str(epoch)
    holder = reserved.get(reservation)
    if (
        holder is not None
        and holder != caller.execution_id
        and _holds_epoch(db, caller.executions, str(holder))
    ):
        raise TeamAdmissionRefused(
            "fresh_reviewer_exists",
            f"execution {holder} is team {team_id}'s epoch {epoch} fresh reviewer",
        )
    _merge(
        caller.executions,
        step,
        fresh_reviewers={**reserved, reservation: caller.execution_id},
    )


async def _create(
    db: HubDatabase,
    caller: _Caller,
    task: Task,
    reviewer_agent: str | None,
    parent: TeamBinding | None = None,
) -> TeamBinding:
    """Record a new team for ``task``, then create its task-titled branch and tree."""
    trees = LocalWorktreeManager(db)
    existing = await asyncio.to_thread(trees.get_by_task, task.id)
    if existing is not None and tree_is_live(existing):
        raise TeamAdmissionRefused(
            "team_exists", f"task {task.id} already has team tree {existing.worktree_path}"
        )
    mode = SINGLE_MODE if parent else await asyncio.to_thread(_admission_mode, db, task)
    try:
        target = await main_checkout_target(db, caller.project_id)
    except LandingGitError as exc:
        raise TeamAdmissionRefused("main_checkout_unavailable", str(exc)) from exc
    if target is None:
        raise TeamAdmissionRefused("main_checkout_unavailable", "main checkout is detached")
    main, base_branch = target
    # An epic team takes its epic branch and directory; a single team keeps the task's
    # generate_branch_name task-<seq>-<title-slug>.
    epic_branch, epic_directory = epic_names(task) if mode == EPIC_MODE else (None, None)
    if parent is not None:
        from gobby.agents.team_dispatch import leaf_names

        await _verify_tree(db, parent)
        base_branch = parent.branch
        epic = await asyncio.to_thread(LocalTaskManager(db).get_task, parent.task_id)
        epic_branch, epic_directory = leaf_names(epic, task)
    config = SpawnConfig(
        prompt="",
        task_id=task.id,
        task_title=task.title,
        task_seq_num=task.seq_num,
        branch_name=epic_branch,
        branch_prefix=None,
        base_branch=base_branch,
        project_id=caller.project_id,
        project_path=str(main),
        provider=caller.session.source,
        parent_session_id=caller.session.id,
        worktree_dir_name=epic_directory,
    )
    branch = generate_branch_name(config)
    taken = await asyncio.to_thread(trees.get_by_branch, caller.project_id, branch)
    if taken is not None and tree_is_live(taken):
        raise TeamAdmissionRefused("team_branch_taken", f"branch {branch} already has a tree")
    intent: dict[str, Any] = {
        "team_id": caller.execution_id,
        "task_id": task.id,
        "project_id": caller.project_id,
        "branch": branch,
        "base_branch": base_branch,
        "reviewers": {reviewer_agent: None} if reviewer_agent is not None else {},
        "mode": mode,
        "parent_team_id": parent.team_id if parent else None,
    }
    await asyncio.to_thread(_record, caller, intent)
    handler = WorktreeIsolationHandler(WorktreeGitManager(str(main)), trees)
    try:
        made = await handler.prepare_environment(config)
    except Exception as exc:
        await handler.cleanup_environment(config)
        raise TeamAdmissionRefused("team_tree_failed", str(exc)) from exc
    handler.commit_environment(config)
    if made.branch_name != branch or made.worktree_id is None:
        raise TeamAdmissionRefused(
            "team_tree_failed", f"tree {made.worktree_id} is not on branch {branch}"
        )
    binding = TeamBinding(**intent, worktree_id=made.worktree_id, worktree_path=made.cwd)
    await asyncio.to_thread(_record, caller, asdict(binding))
    # Seats join this tree without the isolation preflight that normally pins it.
    try:
        await GcodeGateway().pin_project(Path(made.cwd))
    except (GcodeGatewayError, OSError) as exc:
        logger.warning("Failed to pin team code index for %s: %s", made.cwd, exc)
    return binding


async def _create_treeless(
    db: HubDatabase, caller: _Caller, task: Task, reviewer_agent: str | None
) -> TeamBinding:
    """Record a treeless team for ``task``: no branch or tree, its seats work in the main checkout."""
    existing = await asyncio.to_thread(LocalWorktreeManager(db).get_by_task, task.id)
    if existing is not None and tree_is_live(existing):
        raise TeamAdmissionRefused(
            "team_exists", f"task {task.id} already has team tree {existing.worktree_path}"
        )
    mode = await asyncio.to_thread(_admission_mode, db, task)
    if mode == EPIC_MODE:
        raise TeamAdmissionRefused(
            "checkout_mode_epic", f"epic {task.id} needs a team tree; checkout_mode none is single"
        )
    try:
        target = await main_checkout_target(db, caller.project_id)
    except LandingGitError as exc:
        raise TeamAdmissionRefused("main_checkout_unavailable", str(exc)) from exc
    if target is None:
        raise TeamAdmissionRefused("main_checkout_unavailable", "main checkout is detached")
    main, branch = target
    binding = TeamBinding(
        team_id=caller.execution_id,
        task_id=task.id,
        project_id=caller.project_id,
        branch=branch,
        base_branch=branch,
        worktree_id=None,
        worktree_path=str(main),
        reviewers={reviewer_agent: None} if reviewer_agent is not None else {},
        mode=mode,
    )
    await asyncio.to_thread(_record, caller, {**asdict(binding), "checkout_mode": _TREELESS})
    return binding


def _live_treeless_team(db: HubDatabase, project_id: str, task_id: str) -> str | None:
    """The task's treeless team whose teardown has not run yet, or None."""
    rows = db.fetchall(
        """
SELECT * FROM step_executions
WHERE step_id = %s
AND output_json #>> '{team,team_id}' = execution_id::text
AND output_json #>> '{team,task_id}' = %s
AND output_json #>> '{team,checkout_mode}' = %s
AND output_json #>> '{teardown,outcome}' IS NULL
ORDER BY id
""",
        (ADMISSION_STEP, task_id, _TREELESS),
    )
    for row in rows:
        binding = _binding(_recorded(StepExecution.from_row(row)))
        if binding is not None and binding.project_id == project_id:
            return binding.team_id
    return None


def _admission_mode(db: HubDatabase, task: Task) -> str:
    try:
        return admission_mode(db, task)
    except ScopeRefused as exc:
        raise TeamAdmissionRefused(exc.code, str(exc)) from exc


def team_binding(db: HubDatabase, team_id: str) -> TeamBinding | None:
    """Read the canonical admission binding for a team execution."""
    return _binding(_recorded(_admission_step(LocalPipelineExecutionManager(db, None), team_id)))


def task_team(db: HubDatabase, task_id: str) -> TeamBinding | None:
    """The leaf's own team, or its containing epic team for a primary leaf."""
    scoped = live_teams(db, [task_id]).get(task_id) or epic_team_above(db, task_id)
    return team_binding(db, scoped.team_id) if scoped else None


def managed_task_team(db: HubDatabase, caller_id: str, task_id: str) -> TeamBinding | None:
    """Resolve a manager's own team or one of its leaf pods."""
    from gobby.agents.team_dispatch import _is_bound_dev_manager

    manager = seat_team(db, caller_id)
    if manager is None or not _is_bound_dev_manager(db, caller_id, manager.task_id):
        return None
    if task_id != manager.task_id and (
        manager.mode != EPIC_MODE or open_leaf_below(db, manager.task_id, task_id) is None
    ):
        return None
    team = task_team(db, task_id)
    return (
        team
        if team and (team.team_id == manager.team_id or team.parent_team_id == manager.team_id)
        else None
    )


def pod_parent(db: HubDatabase, caller_id: str, task_id: str, parent_id: str) -> TeamBinding:
    """Only an epic's bound manager may admit a pod for an unclaimed open leaf."""
    from gobby.agents.team_dispatch import _is_bound_dev_manager
    from gobby.agents.team_review import seat_team_held

    parent = seat_team(db, caller_id)
    task = LocalTaskManager(db).get_task(task_id)
    if (
        parent is None
        or parent.team_id != parent_id
        or parent.mode != EPIC_MODE
        or (live := live_teams(db, [parent.task_id]).get(parent.task_id)) is None
        or live.team_id != parent.team_id
        or not _is_bound_dev_manager(db, caller_id, parent.task_id)
        or open_leaf_below(db, parent.task_id, task_id) is None
    ):
        raise TeamAdmissionRefused(
            "pod_parent_invalid", "only the epic's Dev Manager admits a leaf pod"
        )
    if seat_team_held(db, parent):
        raise TeamAdmissionRefused("team_held", "the epic is held; resume after its hold is lifted")
    if task.claimed_by_session_id:
        raise TeamAdmissionRefused(
            "pod_leaf_claimed", "release the leaf claim before admitting its pod"
        )
    return parent


async def _verify_tree(db: HubDatabase, binding: TeamBinding) -> None:
    """The team's tree still exists on its recorded branch."""
    if binding.worktree_id is None or binding.worktree_path is None:
        raise TeamAdmissionRefused("team_tree_missing", f"team {binding.team_id} has no tree")
    tree = await asyncio.to_thread(LocalWorktreeManager(db).get, binding.worktree_id)
    if tree is None or not tree_is_live(tree) or tree.branch_name != binding.branch:
        raise TeamAdmissionRefused(
            "team_tree_missing", f"team {binding.team_id} tree {binding.worktree_id} is gone"
        )
    result = await daemon_git.run(("branch", "--show-current"), cwd=binding.worktree_path)
    if not isinstance(result, GitOk) or result.stdout.strip() != binding.branch:
        raise TeamAdmissionRefused(
            "team_tree_missing",
            f"team {binding.team_id} tree {binding.worktree_path} is not on {binding.branch}",
        )


def _caller_binding(db: HubDatabase, caller: Session) -> TeamBinding | None:
    """The caller execution's team, or None when its runbook admits no team."""
    if caller.project_id is None or not caller.external_id.startswith(_PIPELINE_PREFIX):
        return None
    executions = LocalPipelineExecutionManager(db, caller.project_id)
    execution_id = caller.external_id.removeprefix(_PIPELINE_PREFIX)
    execution = executions.get_execution(execution_id)
    if execution is None or not execution.definition_json:
        return None
    try:
        definition = PipelineDefinition.model_validate_json(execution.definition_json)
    except ValueError:
        return None
    if all(step.id != ADMISSION_STEP for step in definition.steps):
        return None
    binding = _binding(_recorded(_admission_step(executions, execution_id)))
    if binding is None:
        raise TeamAdmissionRefused(
            "team_admission_incomplete",
            f"execution {execution_id} launches team seats before its admission completed",
        )
    return binding


def _seat_parents(db: HubDatabase, seat: Session) -> list[str]:
    """The original run's parent first, then resumed runs and the current session parent.

    A resume re-parents the seat to the resumer, so the runbook that launched it sits on
    the resume chain: each resumed run names the run it resumed. The original team
    remains authoritative even when a parent epic's manager resumes a pod seat.
    """
    parents: list[str] = []
    runs = LocalAgentRunManager(db)
    run = runs.get(seat.agent_run_id) if seat.agent_run_id else None
    seen: set[str] = set()
    while run is not None and run.id not in seen:
        seen.add(run.id)
        parents.append(run.parent_session_id)
        previous = (run.resume_metadata_json or {}).get("resume_previous_run_id")
        run = runs.get(previous) if isinstance(previous, str) else None
    parents.reverse()
    if seat.parent_session_id:
        parents.append(seat.parent_session_id)
    return parents


def _pipeline_ancestor(sessions: SessionManager, session_id: str) -> Session | None:
    """The nearest pipeline session at or above ``session_id``, within the agent depth limit."""
    session = sessions.get(session_id)
    for _ in range(_SEAT_DEPTH_LIMIT):
        if session is None:
            return None
        if session.source == _PIPELINE_SOURCE:
            return session
        session = sessions.get(session.parent_session_id) if session.parent_session_id else None
    return None


def seat_team(db: HubDatabase, session_id: str) -> TeamBinding | None:
    """The admitted team whose runbook launched this seat, or None outside any team.

    A leaf developer is spawned by its Dev Manager, so the walk climbs the parent chain to the
    nearest pipeline session, which must be a team runbook. The chain is bounded by the agent
    depth limit.
    """
    sessions = SessionManager(db)
    seat = sessions.get(session_id)
    if seat is None:
        return None
    for parent_id in _seat_parents(db, seat):
        runbook = _pipeline_ancestor(sessions, parent_id)
        if runbook is None:
            continue
        try:
            team = _caller_binding(db, runbook)
        except TeamAdmissionRefused:
            continue
        if team is not None:
            return team
    return None


def bind_reviewer_run(
    db: HubDatabase, team_id: str, project_id: str, definition: str, run_id: str
) -> None:
    """Record ``run_id`` as the live seat of reviewer ``definition`` on team ``team_id``."""
    _bind_run(db, team_id, project_id, "reviewers", definition, run_id)


def bind_primary_run(
    db: HubDatabase, team_id: str, project_id: str, domain: str, run_id: str
) -> None:
    """Record ``run_id`` as the live primary of implementation ``domain`` on team ``team_id``."""
    _bind_run(db, team_id, project_id, "primaries", domain, run_id)


def _bind_run(
    db: HubDatabase, team_id: str, project_id: str, key: str, name: str, run_id: str
) -> None:
    executions = LocalPipelineExecutionManager(db, project_id)
    step = _admission_step(executions, team_id)
    record = _recorded(step)
    if step is None or record is None:
        raise TeamAdmissionRefused("team_missing", f"team {team_id} has no admission record")
    bound = {**(record.get(key) or {}), name: run_id}
    _merge(executions, step, team={**record, key: bound})


def _task_id(db: HubDatabase, ref: str, binding: TeamBinding) -> str | None:
    try:
        return LocalTaskManager(db).resolve_task_reference(ref, binding.project_id)
    except (TaskNotFoundError, ValueError):
        return None


def _tree_id(db: HubDatabase, ref: str) -> str | None:
    try:
        return LocalWorktreeManager(db).resolve_reference(ref)
    except ValueError:
        return None
