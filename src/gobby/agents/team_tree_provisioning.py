"""Team tree provisioning: cut a team's branch and worktree, or adopt its leaf's live tree."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gobby.agents.isolation_models import SpawnConfig, generate_branch_name
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.agents.team_admission_records import (
    ENDED_EXECUTIONS,
    TREELESS,
    Caller,
    TeamAdmissionRefused,
    TeamBinding,
    binding_from_record,
    record_team,
    recorded_team,
    team_binding,
)
from gobby.agents.team_scope import (
    SINGLE_MODE,
    ScopeRefused,
    admission_mode,
    epic_names,
    tree_is_live,
)
from gobby.agents.team_tree_adoption import adoption_refusal
from gobby.code_index.gcode_gateway import GcodeGateway, GcodeGatewayError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.team_records import ADMISSION_STEP, EPIC_MODE
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.tasks.land_commit import LandingGitError, main_checkout_target
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.workflows.pipeline_state import StepExecution
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


async def create_team_tree(
    db: HubDatabase,
    caller: Caller,
    task: Task,
    reviewer_agent: str | None,
    tab_title: str | None,
    parent: TeamBinding | None = None,
) -> TeamBinding:
    """Record a new team for ``task``, then create its task-titled branch and tree.

    A pod adopts the leaf's pre-pod child tree when that tree is its own unteamed pod branch.
    """
    trees = LocalWorktreeManager(db)
    existing = await asyncio.to_thread(trees.get_by_task, task.id)
    if parent is None and existing is not None and tree_is_live(existing):
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
        from gobby.agents.team_scope import leaf_names

        await verify_tree(db, parent)
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
    if parent is not None and existing is not None and tree_is_live(existing):
        # A pod adopts its leaf's tree when no team still holds it; otherwise it is refused.
        return await _adopt_leaf_tree(
            db, caller, task, existing, parent, branch, main, reviewer_agent, tab_title
        )
    taken = await asyncio.to_thread(trees.get_by_branch, caller.project_id, branch)
    if taken is not None and tree_is_live(taken):
        raise TeamAdmissionRefused("team_branch_taken", f"branch {branch} already has a tree")
    intent: dict[str, Any] = {
        "team_id": caller.execution_id,
        "task_id": task.id,
        "project_id": caller.project_id,
        "branch": branch,
        "base_branch": base_branch,
        "reviewers": [reviewer_agent] if reviewer_agent is not None else [],
        "mode": mode,
        "tab_title": tab_title,
        "parent_team_id": parent.team_id if parent else None,
    }
    await asyncio.to_thread(record_team, caller, intent)
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
    await asyncio.to_thread(record_team, caller, asdict(binding))
    await _pin_team_tree(made.cwd)
    return binding


async def _adopt_leaf_tree(
    db: HubDatabase,
    caller: Caller,
    task: Task,
    tree: Worktree,
    parent: TeamBinding,
    leaf_branch: str,
    main: Path,
    reviewer_agent: str | None,
    tab_title: str | None,
) -> TeamBinding:
    """Bind a pod to its leaf's live tree when the tree is clean and no team still holds it."""
    refusal = await adoption_refusal(
        tree, base_branch=parent.branch, leaf_branch=leaf_branch, main=main
    )
    if refusal is None and await _held_by_prior_team(db, tree):
        refusal = "a team admitted on it still holds it"
    if refusal is not None:
        raise TeamAdmissionRefused(
            "team_exists", f"task {task.id} already has team tree {tree.worktree_path}: {refusal}"
        )
    binding = TeamBinding(
        team_id=caller.execution_id,
        task_id=task.id,
        project_id=caller.project_id,
        branch=leaf_branch,
        base_branch=parent.branch,
        worktree_id=tree.id,
        worktree_path=tree.worktree_path,
        reviewers=[reviewer_agent] if reviewer_agent is not None else [],
        mode=SINGLE_MODE,
        tab_title=tab_title,
        parent_team_id=parent.team_id,
    )
    await verify_tree(db, binding)
    await asyncio.to_thread(record_team, caller, asdict(binding))
    await _pin_team_tree(tree.worktree_path)
    return binding


async def _held_by_prior_team(db: HubDatabase, tree: Worktree) -> bool:
    """Whether a team admitted on ``tree`` still holds it.

    A team holds the tree while its execution is in progress, or while a seat of it still works
    in the tree. An ended execution with no live seat leaves the tree free to adopt.
    """
    from gobby.agents.team_teardown import team_seats_live

    executions = LocalPipelineExecutionManager(db, tree.project_id)
    for team_id in await asyncio.to_thread(_prior_team_ids, db, tree.id):
        execution = await asyncio.to_thread(executions.get_execution, team_id)
        if execution is None or execution.status not in ENDED_EXECUTIONS:
            return True
        prior = await asyncio.to_thread(team_binding, db, team_id)
        if prior is None or await asyncio.to_thread(team_seats_live, db, prior):
            return True
    return False


def _prior_team_ids(db: HubDatabase, worktree_id: str) -> list[str]:
    """The ids of the teams admitted on a tree, oldest first."""
    rows = db.fetchall(
        """
SELECT execution_id::text AS team_id FROM step_executions
WHERE step_id = %s
AND output_json #>> '{team,team_id}' = execution_id::text
AND output_json #>> '{team,worktree_id}' = %s
ORDER BY id
""",
        (ADMISSION_STEP, worktree_id),
    )
    return [str(row["team_id"]) for row in rows]


async def _pin_team_tree(path: str) -> None:
    # Seats join this tree without the isolation preflight that normally pins it.
    try:
        await GcodeGateway().pin_project(Path(path))
    except (GcodeGatewayError, OSError) as exc:
        logger.warning("Failed to pin team code index for %s: %s", path, exc)


async def create_treeless_team(
    db: HubDatabase,
    caller: Caller,
    task: Task,
    reviewer_agent: str | None,
    tab_title: str | None,
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
        reviewers=[reviewer_agent] if reviewer_agent is not None else [],
        mode=mode,
        tab_title=tab_title,
    )
    await asyncio.to_thread(record_team, caller, {**asdict(binding), "checkout_mode": TREELESS})
    return binding


def live_treeless_team(db: HubDatabase, project_id: str, task_id: str) -> str | None:
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
        (ADMISSION_STEP, task_id, TREELESS),
    )
    for row in rows:
        binding = binding_from_record(recorded_team(StepExecution.from_row(row)))
        if binding is not None and binding.project_id == project_id:
            return binding.team_id
    return None


def _admission_mode(db: HubDatabase, task: Task) -> str:
    try:
        return admission_mode(db, task)
    except ScopeRefused as exc:
        raise TeamAdmissionRefused(exc.code, str(exc)) from exc


async def verify_tree(db: HubDatabase, binding: TeamBinding) -> None:
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
