"""Single-use teams: one task, one branch and worktree, bound to the execution that admits it.

The team record is the admitting runbook execution: its ``admit`` step output carries the
binding, and ``team_id`` is that execution's id. Admission runs under
``MainCheckoutLanding`` so team admission, landing and the release lease serialize. It
records the team before it creates the tree, so a re-run of a step that died mid-launch
adopts its own tree. A retry of the admitting execution returns the same team; a
replacement execution names the team it joins with ``team_id`` and must match its task.
Seat spawns from a team runbook join the admitted tree and cannot create a branch,
worktree or clone or carry another task.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from gobby.agents.isolation_models import SpawnConfig, generate_branch_name
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.storage.hub.protocol import HubDatabase, MainCheckoutLanding
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.storage.worktrees import LocalWorktreeManager, Worktree, WorktreeStatus
from gobby.tasks.land_commit import LandingGitError, main_checkout_target
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.uuid_validation import parse_uuid_reference
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline_state import ExecutionStatus, StepExecution
from gobby.worktrees.git import WorktreeGitManager

ADMISSION_STEP = "admit"
_PIPELINE_SOURCE = "pipeline"
_PIPELINE_PREFIX = "pipeline-"
_TREE_FIELDS = frozenset({"worktree_id", "worktree_path"})


@dataclass(frozen=True)
class TeamBinding:
    """One admitted team: its task, branch and worktree, and the admitting execution."""

    team_id: str
    task_id: str
    project_id: str
    branch: str
    base_branch: str
    worktree_id: str
    worktree_path: str


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
    db: HubDatabase, *, caller_session_id: str, task: str, team_id: str | None = None
) -> TeamBinding:
    """Admit the caller execution's team for ``task``, or join team ``team_id`` as a replacement."""
    if team_id is not None:
        team_uuid = parse_uuid_reference(team_id)
        if team_uuid is None:
            raise TeamAdmissionRefused("team_missing", f"team {team_id} is not an execution id")
        team_id = str(team_uuid)
    caller = await asyncio.to_thread(_team_caller, db, caller_session_id)
    target = await asyncio.to_thread(_open_task, db, task, caller.project_id)
    async with db.advisory_lock(MainCheckoutLanding(caller.project_id)):
        own = await asyncio.to_thread(_own_record, caller)
        if own is not None:
            return await _readmit(db, caller, own, target, team_id)
        if team_id is not None:
            return await _join(db, caller, target, team_id)
        return await _create(db, caller, target)


def check_team_spawn(db: HubDatabase, caller: Session, target: SeatTarget) -> None:
    """Refuse a seat spawn by a team runbook that would leave its admitted team."""
    binding = _caller_binding(db, caller)
    if binding is None:
        return
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
        Path(target.project_path).resolve() != Path(binding.worktree_path).resolve()
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


def _recorded(step: StepExecution | None) -> dict[str, Any] | None:
    """The team record in an admission step's output, complete or not."""
    try:
        output = json.loads(step.output_json or "null") if step is not None else None
    except json.JSONDecodeError:
        return None
    record = output.get("team") if isinstance(output, dict) else None
    return record if isinstance(record, dict) else None


def _binding(record: dict[str, Any] | None) -> TeamBinding | None:
    if record is None:
        return None
    values: dict[str, str] = {}
    for item in fields(TeamBinding):
        value = record.get(item.name)
        if not isinstance(value, str) or not value:
            return None
        values[item.name] = value
    return TeamBinding(**values)


def _own_record(caller: _Caller) -> dict[str, Any] | None:
    return _recorded(_admission_step(caller.executions, caller.execution_id))


def _record(caller: _Caller, record: dict[str, Any]) -> None:
    caller.executions.update_step_execution(
        caller.step.id, output_json=json.dumps({"team": record})
    )


async def _readmit(
    db: HubDatabase, caller: _Caller, own: dict[str, Any], task: Task, team_id: str | None
) -> TeamBinding:
    """The caller already recorded a team: return it, adopting a tree made before a crash."""
    if own.get("task_id") != task.id or own.get("team_id") != (team_id or caller.execution_id):
        raise TeamAdmissionRefused(
            "team_binding_drift",
            f"execution {caller.execution_id} is bound to team {own.get('team_id')} "
            f"for task {own.get('task_id')}",
        )
    binding = _binding(own)
    if binding is not None:
        await _verify_tree(db, binding)
        return binding
    branch = own.get("branch")
    tree = await asyncio.to_thread(
        LocalWorktreeManager(db).get_by_branch, caller.project_id, str(branch)
    )
    if tree is None or not Path(tree.worktree_path).is_dir():
        return await _create(db, caller, task)
    if tree.task_id != task.id:
        raise TeamAdmissionRefused(
            "team_branch_taken", f"branch {branch} belongs to tree {tree.id} of another task"
        )
    binding = TeamBinding(
        **{key: own[key] for key in own if key not in _TREE_FIELDS},
        worktree_id=tree.id,
        worktree_path=tree.worktree_path,
    )
    await _verify_tree(db, binding)
    await asyncio.to_thread(_record, caller, asdict(binding))
    return binding


async def _join(db: HubDatabase, caller: _Caller, task: Task, team_id: str) -> TeamBinding:
    """A replacement execution joins the team ``team_id`` admitted for the same task."""
    team = await asyncio.to_thread(caller.executions.get_execution, team_id)
    step = await asyncio.to_thread(_admission_step, caller.executions, team_id) if team else None
    binding = _binding(_recorded(step))
    if binding is None or binding.project_id != caller.project_id:
        raise TeamAdmissionRefused("team_missing", f"team {team_id} has no complete admission")
    if binding.task_id != task.id:
        raise TeamAdmissionRefused(
            "team_task_mismatch", f"team {team_id} is bound to task {binding.task_id}"
        )
    await _verify_tree(db, binding)
    await asyncio.to_thread(_record, caller, asdict(binding))
    return binding


async def _create(db: HubDatabase, caller: _Caller, task: Task) -> TeamBinding:
    """Record a new team for ``task``, then create its task-titled branch and tree."""
    trees = LocalWorktreeManager(db)
    existing = await asyncio.to_thread(trees.get_by_task, task.id)
    if existing is not None and _live(existing):
        raise TeamAdmissionRefused(
            "team_exists", f"task {task.id} already has team tree {existing.worktree_path}"
        )
    try:
        target = await main_checkout_target(db, caller.project_id)
    except LandingGitError as exc:
        raise TeamAdmissionRefused("main_checkout_unavailable", str(exc)) from exc
    if target is None:
        raise TeamAdmissionRefused("main_checkout_unavailable", "main checkout is detached")
    main, base_branch = target
    # Task sequence and title only: generate_branch_name derives task-<seq>-<title-slug>.
    config = SpawnConfig(
        prompt="",
        task_id=task.id,
        task_title=task.title,
        task_seq_num=task.seq_num,
        branch_name=None,
        branch_prefix=None,
        base_branch=base_branch,
        project_id=caller.project_id,
        project_path=str(main),
        provider=caller.session.source,
        parent_session_id=caller.session.id,
    )
    branch = generate_branch_name(config)
    taken = await asyncio.to_thread(trees.get_by_branch, caller.project_id, branch)
    if taken is not None and _live(taken):
        raise TeamAdmissionRefused("team_branch_taken", f"branch {branch} already has a tree")
    intent = {
        "team_id": caller.execution_id,
        "task_id": task.id,
        "project_id": caller.project_id,
        "branch": branch,
        "base_branch": base_branch,
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
    return binding


def _live(tree: Worktree) -> bool:
    return tree.status == WorktreeStatus.ACTIVE.value and Path(tree.worktree_path).is_dir()


async def _verify_tree(db: HubDatabase, binding: TeamBinding) -> None:
    """The team's tree still exists on its recorded branch."""
    tree = await asyncio.to_thread(LocalWorktreeManager(db).get, binding.worktree_id)
    if tree is None or not _live(tree) or tree.branch_name != binding.branch:
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
