"""Create worktree tool handler."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import (
    copy_project_json_to_worktree,
    generate_worktree_path,
    install_provider_hooks,
    resolve_project_context,
)

logger = logging.getLogger(__name__)


def create_create_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with the create_worktree tool.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with create_worktree registered
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-create",
        description="Worktree creation",
    )

    @registry.tool(
        name="create_worktree",
        description="Create a new git worktree for isolated development.",
    )
    async def create_worktree(
        branch_name: str,
        base_branch: str = "main",
        task_id: str | None = None,
        worktree_path: str | None = None,
        create_branch: bool = True,
        use_local: bool | None = None,
        project_path: str | None = None,
        provider: Literal["claude", "gemini", "qwen", "codex"] | None = None,
    ) -> dict[str, Any]:
        """Create a new git worktree.

        Args:
            branch_name: Name for the new branch.
            base_branch: Branch to base the worktree on (default: main).
            task_id: Optional task ID to link to this worktree.
            worktree_path: Optional custom path (defaults to ../{branch_name}).
            create_branch: Whether to create a new branch (default: True).
            use_local: If True, branch from local ref instead of origin/ (preserves unpushed commits).
                       If None (default), auto-detects: uses local when base_branch has unpushed commits.
            project_path: Path to project directory (pass cwd from CLI).
            provider: CLI provider to install hooks for (claude, gemini, qwen, codex).
                     If specified, installs hooks so agents can communicate with daemon.

        Returns:
            Dict with worktree ID, path, and branch info.
        """
        resolved_git_mgr, resolved_project_id, error = resolve_project_context(
            project_path, ctx.git_manager, ctx.project_id
        )
        if error:
            return {"success": False, "error": error}

        if resolved_git_mgr is None or resolved_project_id is None:
            raise RuntimeError("Git manager or project ID unexpectedly None")

        # Check if branch already exists as a worktree
        existing = ctx.worktree_storage.get_by_branch(resolved_project_id, branch_name)
        if existing:
            return {
                "success": False,
                "error": f"Worktree already exists for branch '{branch_name}'",
                "existing_worktree_id": existing.id,
                "existing_path": existing.worktree_path,
            }

        # Generate default worktree path if not provided
        if worktree_path is None:
            project_name = Path(resolved_git_mgr.repo_path).name
            worktree_path = generate_worktree_path(branch_name, project_name)
        else:
            worktree_path = str(Path(worktree_path).expanduser())

        # Auto-detect use_local when not explicitly set
        resolved_use_local = use_local
        if resolved_use_local is None and create_branch:
            try:
                has_unpushed, unpushed_count = await asyncio.to_thread(
                    resolved_git_mgr.has_unpushed_commits, base_branch
                )
                if has_unpushed:
                    resolved_use_local = True
                    logger.info(
                        f"Auto-detected {unpushed_count} unpushed commit(s) on '{base_branch}', using local branch ref",
                    )
            except Exception as e:
                logger.warning(f"Auto-detect unpushed commits failed: {e}")
        if resolved_use_local is None:
            resolved_use_local = False

        # Create git worktree
        result = await asyncio.to_thread(
            resolved_git_mgr.create_worktree,
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_branch=base_branch,
            create_branch=create_branch,
            use_local=resolved_use_local,
        )

        if not result.success:
            return {"success": False, "error": result.error or "Failed to create git worktree"}

        # Resolve task_id (#N -> UUID) before DB insert
        resolved_task_id = None
        if task_id:
            try:
                resolved_task_id = ctx.resolve_task_id(task_id)
            except ValueError as e:
                # Clean up the git worktree we just created
                try:
                    await asyncio.to_thread(
                        resolved_git_mgr.delete_worktree,
                        worktree_path,
                        force=True,
                        delete_branch=True,
                        branch_name=branch_name,
                    )
                except Exception as cleanup_err:
                    logger.warning(
                        f"Failed to clean up worktree after task resolution failure: {cleanup_err}"
                    )
                return {"success": False, "error": f"Invalid task reference: {e}"}

        # Record in database -- clean up git worktree on failure
        try:
            worktree = ctx.worktree_storage.create(
                project_id=resolved_project_id,
                branch_name=branch_name,
                worktree_path=worktree_path,
                base_branch=base_branch,
                task_id=resolved_task_id,
            )
        except Exception as db_err:
            try:
                await asyncio.to_thread(
                    resolved_git_mgr.delete_worktree,
                    worktree_path,
                    force=True,
                    delete_branch=True,
                    branch_name=branch_name,
                )
            except Exception as cleanup_err:
                logger.warning(
                    f"Failed to clean up orphaned worktree {worktree_path}: {cleanup_err}"
                )
            return {"success": False, "error": f"Failed to record worktree in database: {db_err}"}

        # Copy project.json and install provider hooks
        hooks_installed = False
        try:
            copy_project_json_to_worktree(resolved_git_mgr.repo_path, worktree.worktree_path)
            hooks_installed = install_provider_hooks(provider, worktree.worktree_path)
        except Exception as post_err:
            logger.warning(f"Post-creation setup failed for worktree {worktree.id}: {post_err}")

        return {
            "success": True,
            "worktree_id": worktree.id,
            "worktree_path": worktree.worktree_path,
            "hooks_installed": hooks_installed,
        }

    return registry
