"""Reverse reconciliation for unmanaged worktrees and clones."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.clones import git as clone_git
from gobby.clones.git import CloneGitManager
from gobby.runner_maintenance_helpers import _run_db
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase, IsolationRegistryReconciliation
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.workspace_machine_scope import require_local_machine_id
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.git import _status as worktree_git_status

logger = logging.getLogger(__name__)

_IGNORED_NAME_PREFIXES = ("_orphaned", "_migrated")


@dataclass(frozen=True)
class IsolationReconciliationResult:
    """Counts of newly registered isolation workspaces."""

    worktrees_adopted: int = 0
    clones_adopted: int = 0

    @property
    def total_adopted(self) -> int:
        return self.worktrees_adopted + self.clones_adopted


async def reconcile_isolation_registry(
    db: HubDatabase,
    *,
    machine_id: str | None = None,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> IsolationReconciliationResult:
    """Adopt unmanaged isolation workspaces for registered local projects."""
    resolved_machine_id = require_local_machine_id(
        machine_id,
        resource_kind="isolation_registry",
        resource_id=machine_id or "local",
    )
    lock = IsolationRegistryReconciliation(machine_id=resolved_machine_id)

    async with db.advisory_lock(lock):
        result = await _reconcile_isolation_registry(
            db,
            resolved_machine_id,
            run_db=run_db,
        )

    if result.total_adopted:
        logger.info(
            "Isolation registry reconciliation adopted worktrees=%d clones=%d machine_id=%s",
            result.worktrees_adopted,
            result.clones_adopted,
            resolved_machine_id,
        )
    return result


async def _reconcile_isolation_registry(
    db: HubDatabase,
    machine_id: str,
    *,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> IsolationReconciliationResult:
    project_storage = LocalProjectManager(db)
    checkout_storage = LocalProjectCheckoutManager(db)
    worktree_storage = LocalWorktreeManager(db)
    clone_storage = LocalCloneManager(db)
    checkouts = await _run_db(run_db, checkout_storage.list_for_machine, machine_id)
    worktrees_adopted = 0
    clones_adopted = 0

    for checkout in checkouts:
        project = await _run_db(run_db, project_storage.get, checkout.project_id)
        if project is None or _is_ignored_name(project.name):
            continue
        worktrees_adopted += await _reconcile_project_worktrees(
            project,
            checkout.root_path,
            worktree_storage,
            run_db=run_db,
        )
        clones_adopted += await _reconcile_project_clones(
            project,
            checkout.root_path,
            clone_storage,
            run_db=run_db,
        )

    return IsolationReconciliationResult(
        worktrees_adopted=worktrees_adopted,
        clones_adopted=clones_adopted,
    )


async def _reconcile_project_worktrees(
    project: Project,
    checkout_root: str,
    storage: LocalWorktreeManager,
    *,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> int:
    manager = WorktreeGitManager(checkout_root)
    try:
        primary_path = await asyncio.to_thread(_canonical_path, checkout_root)
        worktrees = await asyncio.to_thread(
            worktree_git_status.list_worktrees,
            manager,
            failure_log_level=logging.DEBUG,
        )
        base_branch = await asyncio.to_thread(manager.get_default_branch)
    except Exception as exc:
        logger.debug("Skipping worktree reconciliation for %s: %s", project.name, exc)
        return 0

    adopted = 0
    for worktree in worktrees:
        if worktree.is_bare or worktree.prunable or _is_ignored_name(Path(worktree.path).name):
            continue
        try:
            candidate_path = await asyncio.to_thread(_canonical_path, worktree.path)
            if candidate_path == primary_path:
                continue
            inspected = await asyncio.to_thread(manager.inspect_worktree, candidate_path)
            _, created = await _run_db(
                run_db,
                storage.register_adopted,
                project.id,
                inspected.branch,
                inspected.path,
                base_branch,
            )
            adopted += int(created)
        except Exception as exc:
            logger.debug(
                "Skipping worktree reconciliation candidate %s for %s: %s",
                worktree.path,
                project.name,
                exc,
            )
    return adopted


async def _reconcile_project_clones(
    project: Project,
    checkout_root: str,
    storage: LocalCloneManager,
    *,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> int:
    manager = CloneGitManager(checkout_root)
    try:
        project_directory = await asyncio.to_thread(
            _canonical_path,
            clone_git.CLONES_ROOT / project.name,
        )
        candidates = await asyncio.to_thread(_list_immediate_directories, project_directory)
    except OSError as exc:
        logger.debug("Skipping clone reconciliation for %s: %s", project.name, exc)
        return 0

    if not candidates:
        return 0
    base_branch = await asyncio.to_thread(manager.get_default_branch)

    adopted = 0
    for candidate in candidates:
        try:
            resolved_path = await asyncio.to_thread(manager.resolve_managed_clone_path, candidate)
            if resolved_path is None or resolved_path.parent != project_directory:
                continue
            status = await asyncio.to_thread(manager.get_clone_status, resolved_path)
            if status is None or (status.branch is None and status.commit is None):
                continue
            remote_url = await asyncio.to_thread(
                manager.get_remote_url,
                "origin",
                resolved_path,
            )
            _, created = await _run_db(
                run_db,
                storage.register_adopted,
                project.id,
                status.branch,
                str(resolved_path),
                base_branch,
                remote_url,
            )
            adopted += int(created)
        except Exception as exc:
            logger.debug(
                "Skipping clone reconciliation candidate %s for %s: %s",
                candidate,
                project.name,
                exc,
            )
    return adopted


def _canonical_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _list_immediate_directories(path: Path) -> tuple[Path, ...]:
    if not path.is_dir():
        return ()
    return tuple(child for child in path.iterdir() if child.is_dir())


def _is_ignored_name(name: str) -> bool:
    return name.startswith(_IGNORED_NAME_PREFIXES)
