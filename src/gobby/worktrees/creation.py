"""Shared orchestration for creating Git worktrees."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.utils import project_context
from gobby.utils.project_context import IsolationProjectJsonError
from gobby.worktrees import events
from gobby.worktrees.base_branch import rejects_unreferenced_sha_base
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)

type Provider = Literal["claude", "qwen", "codex", "droid"]
type PathFactory = Callable[[str, str | None], str]
type SidecarWriter = Callable[[str | Path, str | Path], Awaitable[None]]
type HookInstaller = Callable[[Provider | None, str | Path], bool]


class EventEmitter(Protocol):
    """Callable shape for worktree lifecycle event publishers."""

    def __call__(self, event_type: str, **payload: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorktreeCreationResult:
    """Typed result for expected worktree creation outcomes."""

    success: bool
    worktree: Worktree | None = None
    error: str | None = None
    error_code: str | None = None
    existing_worktree_id: str | None = None
    existing_path: str | None = None
    hooks_installed: bool = False
    event: dict[str, Any] | None = None


def generate_worktree_path(branch_name: str, project_name: str | None = None) -> str:
    """Generate the durable default path for a worktree."""
    base = Path.home() / ".gobby" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    safe_branch = re.sub(r'[\\:*?"<>|\s/]', "-", branch_name)
    safe_branch = re.sub(r"-{2,}", "-", safe_branch).strip("-") or "unnamed-branch"
    return str(base / project_name / safe_branch) if project_name else str(base / safe_branch)


async def create_worktree(
    *,
    git_manager: WorktreeGitManager,
    worktree_storage: LocalWorktreeManager,
    project_id: str,
    branch_name: str,
    base_branch: str = "main",
    task_id: str | None = None,
    worktree_path: str | None = None,
    create_branch: bool = True,
    use_local: bool | None = None,
    workspace_role: str = "task",
    provider: Provider | None = None,
    resolve_task_id: Callable[[str], str] | None = None,
    path_factory: PathFactory | None = None,
    sidecar_writer: SidecarWriter | None = None,
    hook_installer: HookInstaller | None = None,
    event_emitter: EventEmitter | None = None,
) -> WorktreeCreationResult:
    """Create, persist, initialize, and announce a worktree."""
    if base_branch.startswith(("origin/", "refs/remotes/")):
        return WorktreeCreationResult(
            success=False,
            error=f"Remote-style base branch is not allowed: {base_branch}",
            error_code="remote_base_branch_not_allowed",
        )
    try:
        if await rejects_unreferenced_sha_base(git_manager, base_branch):
            return WorktreeCreationResult(
                success=False,
                error=f"base_branch must be a branch name, not a commit sha: {base_branch}",
                error_code="base_branch_is_commit_sha",
            )
    except RuntimeError as exc:
        return WorktreeCreationResult(
            success=False,
            error=f"Unable to verify base_branch '{base_branch}': {exc}",
            error_code="base_branch_refs_unavailable",
        )

    existing = await asyncio.to_thread(worktree_storage.get_by_branch, project_id, branch_name)
    if existing:
        return WorktreeCreationResult(
            success=False,
            error=f"Worktree already exists for branch '{branch_name}'",
            error_code="branch_conflict",
            existing_worktree_id=existing.id,
            existing_path=existing.worktree_path,
        )

    if worktree_path is None:
        factory = path_factory or generate_worktree_path
        worktree_path = await asyncio.to_thread(
            factory, branch_name, Path(git_manager.repo_path).name
        )
    # Overlay grants and index identities use the canonical checkout root.
    worktree_path = str(Path(worktree_path).expanduser().resolve())

    resolved_use_local = use_local
    if resolved_use_local is None and create_branch:
        try:
            has_unpushed, unpushed_count = await git_manager.has_unpushed_commits(base_branch)
            if has_unpushed:
                resolved_use_local = True
                logger.info(
                    "Auto-detected %s unpushed commit(s) on '%s', using local branch ref",
                    unpushed_count,
                    base_branch,
                )
        except Exception as exc:
            return WorktreeCreationResult(
                success=False,
                error=f"Unable to safely select worktree base for '{base_branch}': {exc}",
                error_code="branch_divergence_unavailable",
            )
    if resolved_use_local is None:
        resolved_use_local = False

    git_result = await git_manager.create_worktree(
        worktree_path=worktree_path,
        branch_name=branch_name,
        base_branch=base_branch,
        create_branch=create_branch,
        use_local=resolved_use_local,
    )
    if not git_result.success:
        return WorktreeCreationResult(
            success=False,
            error=git_result.error or "Failed to create git worktree",
            error_code="git_create_failed",
        )

    resolved_task_id = None
    if task_id:
        if resolve_task_id is None:
            raise RuntimeError("A task resolver is required when task_id is provided")
        try:
            resolved_task_id = await asyncio.to_thread(resolve_task_id, task_id)
        except ValueError as exc:
            await _cleanup_git_worktree(
                git_manager,
                worktree_path,
                branch_name,
                create_branch=create_branch,
                context="task resolution failure",
            )
            return WorktreeCreationResult(
                success=False,
                error=f"Invalid task reference: {exc}",
                error_code="invalid_task_reference",
            )

    try:
        worktree = await asyncio.to_thread(
            worktree_storage.create,
            project_id=project_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            base_branch=base_branch,
            task_id=resolved_task_id,
            workspace_role=workspace_role,
        )
    except Exception as exc:
        await _cleanup_git_worktree(
            git_manager,
            worktree_path,
            branch_name,
            create_branch=create_branch,
            context="database failure",
        )
        return WorktreeCreationResult(
            success=False,
            error=f"Failed to record worktree in database: {exc}",
            error_code="storage_create_failed",
        )

    writer = sidecar_writer or project_context.ensure_project_json_for_isolation
    try:
        await writer(git_manager.repo_path, worktree.worktree_path)
    except (IsolationProjectJsonError, OSError) as exc:
        await _cleanup_git_worktree(
            git_manager,
            worktree.worktree_path,
            branch_name,
            create_branch=create_branch,
            context="isolation sidecar failure",
        )
        try:
            await asyncio.to_thread(worktree_storage.delete, worktree.id)
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to delete worktree record after isolation sidecar failure: %s",
                cleanup_exc,
            )
        return WorktreeCreationResult(
            success=False,
            error=f"Failed to write isolation marker: {exc}",
            error_code="isolation_marker_failed",
        )

    hooks_installed = False
    if hook_installer is not None:
        try:
            hooks_installed = await asyncio.to_thread(
                hook_installer, provider, worktree.worktree_path
            )
        except Exception as exc:
            logger.warning(
                "Post-creation hook install failed for worktree %s: %s", worktree.id, exc
            )

    event = None
    emit = event_emitter or events.emit_worktree_event
    try:
        event = emit(
            "worktree_created",
            worktree_id=worktree.id,
            project_id=worktree.project_id,
            branch_name=worktree.branch_name,
            worktree_path=worktree.worktree_path,
            base_branch=worktree.base_branch,
            task_id=worktree.task_id,
        )
    except Exception as exc:
        logger.warning(
            "Failed to emit worktree_created event for %s at %s: %s",
            worktree.id,
            worktree.worktree_path,
            exc,
            exc_info=True,
        )

    return WorktreeCreationResult(
        success=True,
        worktree=worktree,
        hooks_installed=hooks_installed,
        event=event,
    )


async def _cleanup_git_worktree(
    git_manager: WorktreeGitManager,
    worktree_path: str,
    branch_name: str,
    *,
    create_branch: bool,
    context: str,
) -> None:
    """Best-effort rollback for a worktree not returned to a caller."""
    try:
        await git_manager.delete_worktree(
            worktree_path,
            force=True,
            delete_branch=create_branch,
            force_delete_branch=create_branch,
            branch_name=branch_name,
        )
    except Exception as exc:
        logger.warning("Failed to clean up worktree after %s: %s", context, exc)
