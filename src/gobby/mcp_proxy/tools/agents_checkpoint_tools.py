"""Coordinator-only recovery checkpoints for terminal agent worktrees."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from gobby.agents.worktree_checkpoint import WorktreeCheckpointError, checkpoint_worktree
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    inspect_checkout_path_ownership,
)
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_edited_path,
    task_edited_file_set_for_checkout,
)

_ACTIVE_RUN_STATUSES = {"pending", "running"}
logger = logging.getLogger(__name__)


def register_agent_checkpoint_tools(
    registry: InternalToolRegistry,
    ctx: AgentsRegistryContext,
) -> None:
    @registry.tool(
        name="checkpoint_agent_worktree",
        description=(
            "Checkpoint a terminal child agent's task-owned isolated worktree for reuse. "
            "Only the run's parent coordinator may call this operation; it never releases "
            "a task claim or terminates an agent run."
        ),
    )
    async def checkpoint_agent_worktree(run_id: str) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        if not caller_ref:
            return _error("No active coordinator session context", "session_required")
        try:
            caller_session_id = ctx.resolve_session_id(caller_ref)
        except ValueError as exc:
            return _error(str(exc), "session_invalid")
        try:
            return await asyncio.to_thread(
                _checkpoint_agent_worktree,
                ctx,
                run_id=run_id,
                caller_session_id=caller_session_id,
            )
        except Exception as exc:
            logger.exception("Unexpected worktree checkpoint failure for run %s", run_id)
            return _error(f"Checkpoint failed: {exc}", "checkpoint_failed")


def _checkpoint_agent_worktree(
    ctx: AgentsRegistryContext,
    *,
    run_id: str,
    caller_session_id: str,
) -> dict[str, Any]:
    if ctx.task_manager is None or ctx.worktree_storage is None:
        return _error("Task and worktree services are required", "checkpoint_unavailable")

    run = ctx.agent_run_manager.get(run_id)
    if run is None:
        return _error(f"Agent run {run_id} not found", "run_not_found")
    if run.status in _ACTIVE_RUN_STATUSES:
        return _error(
            f"Agent run {run_id} is still {run.status}; terminate it before checkpointing",
            "run_active",
        )
    if run.parent_session_id != caller_session_id:
        return _error(
            "Only the terminal run's parent coordinator may checkpoint its worktree",
            "coordinator_required",
        )
    if not run.task_id or not run.child_session_id:
        return _error("Agent run has no task or child session boundary", "invalid_agent_boundary")
    if not run.worktree_id:
        return _error(
            "Agent run did not use an isolated worktree",
            "isolated_worktree_required",
        )

    task = ctx.task_manager.get_task(run.task_id)
    if task is None:
        return _error(f"Task {run.task_id} not found", "task_not_found")
    if task.seq_num is None:
        return _error("Task has no project sequence number", "task_not_found")
    if is_task_closed(task):
        return _error("Closed tasks cannot receive recovery checkpoints", "task_closed")
    task_owner = get_claimed_session_id(task)
    allowed_task_owners = {None, caller_session_id, run.child_session_id}
    if task_owner not in allowed_task_owners:
        return _error(
            "The task is owned by a session outside this coordinator recovery boundary",
            "foreign_task_owner",
        )

    active_task_run = ctx.agent_run_manager.get_active_run_for_task(run.task_id)
    if active_task_run is not None:
        return _error(
            f"Task already has active agent run {active_task_run.id}",
            "run_active",
        )
    active_worktree_run = ctx.agent_run_manager.get_active_run_for_worktree(run.worktree_id)
    if active_worktree_run is not None:
        return _error(
            f"Worktree already has active agent run {active_worktree_run.id}",
            "run_active",
        )

    worktree = ctx.worktree_storage.get(run.worktree_id)
    if worktree is None:
        return _error(f"Worktree {run.worktree_id} not found", "worktree_not_found")
    if (
        worktree.task_id != run.task_id
        or worktree.project_id != task.project_id
        or worktree.workspace_role != "task"
        or worktree.status != "active"
    ):
        return _error(
            "Agent run is not bound to an active task-isolation worktree",
            "isolated_worktree_required",
        )
    if run.machine_id != worktree.machine_id:
        return _error("Agent run and worktree belong to different machines", "foreign_worktree")

    git_manager = _resolve_git_manager(ctx, worktree.project_id)
    if git_manager is None:
        return _error(
            "No Git manager is available for the worktree project", "checkpoint_unavailable"
        )
    try:
        inspected = git_manager.inspect_worktree(worktree.worktree_path)
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(f"Worktree inspection failed: {exc}", "isolated_worktree_required")
    if (
        Path(inspected.path).resolve() != Path(worktree.worktree_path).resolve()
        or inspected.branch is None
        or inspected.branch != worktree.branch_name
        or inspected.is_bare
        or inspected.is_detached
        or inspected.prunable
    ):
        return _error(
            "Registered worktree does not match its linked branch checkout",
            "isolated_worktree_required",
        )

    allowed_worktree_owners = tuple(
        owner for owner in (caller_session_id, run.child_session_id) if owner is not None
    )
    try:
        claimed = ctx.worktree_storage.claim_if_available(
            worktree.id,
            caller_session_id,
            allowed_existing_session_ids=allowed_worktree_owners,
        )
    except MachineOwnershipMismatchError as exc:
        return exc.to_dict()
    if claimed is None:
        return _error(
            "Worktree is owned by a different session",
            "foreign_worktree_owner",
        )

    checkpoint = None
    failure: dict[str, Any] | None = None
    release_error: str | None = None
    try:
        db = ctx.db or ctx.task_manager.db
        checkout_root = str(Path(worktree.worktree_path).resolve())
        ownership = inspect_checkout_path_ownership(
            db,
            project_id=worktree.project_id,
            checkout_root=checkout_root,
        )
        dirty_paths = {item.path for item in ownership}
        task_ref = f"#{task.seq_num}"
        foreign_paths = sorted(
            item.path
            for item in ownership
            if any(owner.task_ref != task_ref for owner in item.owners)
        )
        if foreign_paths:
            failure = _error(
                f"Worktree contains foreign-attributed paths: {', '.join(foreign_paths)}",
                "foreign_attributed_paths",
                paths=foreign_paths,
            )
        else:
            authorized_paths = _authorized_task_paths(
                db,
                task_id=task.id,
                checkout_root=checkout_root,
                session_ids={caller_session_id, run.child_session_id},
                legacy_child_session_id=run.child_session_id if task_owner is None else None,
            )
            unattributed_paths = sorted(dirty_paths - authorized_paths)
            if unattributed_paths:
                failure = _error(
                    f"Worktree contains unattributed paths: {', '.join(unattributed_paths)}",
                    "unattributed_paths",
                    paths=unattributed_paths,
                )
            else:
                checkpoint = checkpoint_worktree(
                    worktree_path=checkout_root,
                    expected_paths=dirty_paths,
                    task_seq_num=task.seq_num,
                    run_id=run.id,
                )
    except DirtyEditOwnershipInspectionError as exc:
        failure = _error(f"Path ownership inspection failed: {exc}", "ownership_inspection_failed")
    except WorktreeCheckpointError as exc:
        failure = _error(str(exc), exc.error_code)
    except (OSError, RuntimeError, ValueError) as exc:
        failure = _error(f"Checkpoint failed: {exc}", "checkpoint_failed")
    finally:
        try:
            released = ctx.worktree_storage.release(worktree.id)
        except Exception as exc:
            logger.exception("Failed to release checkpoint claim for worktree %s", worktree.id)
            released = None
            release_error = str(exc)

    if released is None:
        response = _error(
            "Checkpoint finished but the coordinator worktree claim could not be released"
            + (f": {release_error}" if release_error else ""),
            "worktree_release_failed",
        )
        if checkpoint is not None:
            response["commit_sha"] = checkpoint.commit_sha
            response["included_paths"] = list(checkpoint.included_paths)
        return response
    if failure is not None:
        return failure
    assert checkpoint is not None
    return {
        "success": True,
        "run_id": run.id,
        "task_id": task.id,
        "task_ref": task_ref,
        "worktree_id": worktree.id,
        "worktree_path": str(Path(worktree.worktree_path).resolve()),
        "branch_name": worktree.branch_name,
        "commit_sha": checkpoint.commit_sha,
        "included_paths": list(checkpoint.included_paths),
        "commit_message": checkpoint.message,
        "worktree_released": True,
    }


def _authorized_task_paths(
    db: Any,
    *,
    task_id: str,
    checkout_root: str,
    session_ids: set[str],
    legacy_child_session_id: str | None,
) -> set[str]:
    """Return task paths, including the narrow pre-#21897 terminal recovery case.

    Legacy terminal cleanup erased all task ledgers after releasing the child's
    claim, but retained that child's session edit ledger. The caller supplies the
    child only for an unclaimed recovered task; current or still-owned task states
    continue to require checkout-scoped task attribution.
    """
    variable_manager = SessionVariableManager(db)
    variables_by_session: dict[str, dict[str, Any]] = {}
    authorized: set[str] = set()
    for session_id in session_ids:
        variables = variable_manager.get_variables(session_id)
        variables_by_session[session_id] = variables
        authorized.update(task_edited_file_set_for_checkout(variables, task_id, checkout_root))

    if legacy_child_session_id is not None:
        legacy_variables = variables_by_session.get(legacy_child_session_id)
        if legacy_variables is None:
            legacy_variables = variable_manager.get_variables(legacy_child_session_id)
        task_ledgers = (
            legacy_variables.get("task_edited_files"),
            legacy_variables.get("task_edited_file_checkouts"),
            legacy_variables.get("task_edited_file_times"),
        )
        attribution_was_cleared = all(
            not isinstance(ledger, dict) or task_id not in ledger for ledger in task_ledgers
        )
        raw_session_paths = legacy_variables.get("session_edited_files")
        if attribution_was_cleared and isinstance(raw_session_paths, list):
            authorized.update(
                path
                for value in raw_session_paths
                if (path := normalize_task_edited_path(value)) is not None
            )
    return authorized


def _resolve_git_manager(ctx: AgentsRegistryContext, project_id: str) -> Any | None:
    if ctx.git_manager_resolver is not None:
        try:
            return ctx.git_manager_resolver(project_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
    return ctx.git_manager


def _error(message: str, error_code: str, **details: Any) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": error_code, **details}
