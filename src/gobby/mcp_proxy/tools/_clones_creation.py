"""Clone creation MCP tools."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from gobby.agents.frozen_workspace import frozen_workspace_refusal
from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.git import run_to_completion
from gobby.worktrees.base_branch import WorktreeBaseError, select_worktree_base
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


def create_clone_creation_registry(ctx: CloneRegistryContext) -> InternalToolRegistry:
    """Create a registry with clone creation tools."""
    registry = InternalToolRegistry(
        name="gobby-clones-creation",
        description="Clone creation tools",
    )

    async def _create_clone_impl(
        branch_name: str,
        clone_path: str,
        remote_url: str | None = None,
        task_id: str | None = None,
        base_branch: str | None = None,
        depth: int = 1,
        cancellation_requested: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """
        Create a new git clone.

        Args:
            branch_name: Branch to create (external clones check it out)
            clone_path: Path where clone will be created
            remote_url: External repository to shallow-clone; omitted clones
                the local repository at the selected commit
            task_id: Optional task ID to link
            base_branch: Local fork base (omitted: the caller's HEAD)
            depth: External clone depth (default: 1)

        Returns:
            Dict with clone info, base_commit_sha and index_source, or error
        """
        # Expand ~ before any filesystem operations (subprocess.run doesn't expand tildes)
        clone_path = str(Path(clone_path).expanduser())

        git_manager = ctx.git_manager
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        # clones.project_id is a NOT NULL uuid column; refuse before doing git work
        # rather than failing the DB insert after the clone directory exists.
        project_id = ctx.project_id
        if not project_id:
            return {
                "success": False,
                "error": "No project context available for clone creation",
            }

        clone_created = False
        record_created = False

        async def _cleanup_created_clone() -> None:
            if not clone_created or record_created:
                return
            try:
                cleanup_result = await git_manager.delete_clone(clone_path, force=True)
                if not cleanup_result.success:
                    logger.warning(
                        "Failed to clean up clone %s after create failure: %s",
                        clone_path,
                        cleanup_result.error or cleanup_result.message,
                    )
            except Exception:
                logger.warning(
                    "Failed to clean up clone %s after create failure",
                    clone_path,
                    exc_info=True,
                )

        try:
            # Resolve task references before creating anything on disk. A bad task
            # reference must not leave a clone directory behind.
            resolved_task_id = ctx.resolve_task_id(task_id) if task_id else None
            refusal = await asyncio.to_thread(
                frozen_workspace_refusal,
                ctx.clone_storage.db,
                "create_clone",
                task_id=resolved_task_id,
            )
            if refusal is not None:
                return refusal
            if cancellation_requested is not None and cancellation_requested.is_set():
                raise asyncio.CancelledError

            if remote_url:
                # External repository: no local index to reuse
                index_source = "cold"
                stored_base = base_branch or "main"
                result = await git_manager.shallow_clone(
                    remote_url=remote_url,
                    clone_path=clone_path,
                    branch=branch_name,
                    depth=depth,
                )
                clone_created = result.success
                if result.success:
                    head = await git_manager.run_git_command(
                        ["rev-parse", "HEAD"], cwd=clone_path, check=True
                    )
                    base_commit_sha = head.stdout.strip()
            else:
                index_source = "local"
                try:
                    stored_base, base_commit_sha = await select_worktree_base(
                        WorktreeGitManager(git_manager.repo_path), base_branch
                    )
                except WorktreeBaseError as error:
                    return {"success": False, "error": str(error), "error_code": error.code}
                if cancellation_requested is not None and cancellation_requested.is_set():
                    raise asyncio.CancelledError
                result = await git_manager.create_clone(
                    clone_path,
                    branch_name,
                    stored_base,
                    base_commit_sha=base_commit_sha,
                )
                clone_created = result.success
                remote_url = await git_manager.get_remote_url() or str(git_manager.repo_path)

            if not result.success:
                return {
                    "success": False,
                    "error": f"Clone failed: {result.error or result.message}",
                }

            # Store clone record
            clone = ctx.clone_storage.create(
                project_id=project_id,
                branch_name=branch_name,
                clone_path=clone_path,
                base_branch=stored_base,
                task_id=resolved_task_id,
                remote_url=remote_url,
            )
            record_created = True

            return {
                "success": True,
                "clone": clone.to_dict(),
                "base_commit_sha": base_commit_sha,
                "index_source": index_source,
                "message": f"Created clone at {clone_path}",
            }

        except asyncio.CancelledError:
            await _cleanup_created_clone()
            raise
        except Exception as e:
            await _cleanup_created_clone()
            logger.exception("Error creating clone: %s", e)
            return {"success": False, "error": str(e)}

    async def create_clone(
        branch_name: str,
        clone_path: str,
        remote_url: str | None = None,
        task_id: str | None = None,
        base_branch: str | None = None,
        depth: int = 1,
    ) -> dict[str, Any]:
        cancellation_requested = asyncio.Event()
        return await run_to_completion(
            _create_clone_impl(
                branch_name,
                clone_path,
                remote_url,
                task_id,
                base_branch,
                depth,
                cancellation_requested,
            ),
            on_cancel=cancellation_requested.set,
        )

    registry.register(
        name="create_clone",
        description="Create a new git clone for isolated development",
        input_schema={
            "type": "object",
            "properties": {
                "branch_name": {
                    "type": "string",
                    "description": ("Branch to create in the clone (external clones check it out)"),
                },
                "clone_path": {
                    "type": "string",
                    "description": "Path where clone will be created",
                },
                "remote_url": {
                    "type": "string",
                    "description": (
                        "External repository to shallow-clone; omit to clone the local repository"
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional task ID to link",
                },
                "base_branch": {
                    "type": "string",
                    "description": (
                        "Local fork base: a branch or origin/<branch> (omitted: the caller's HEAD)"
                    ),
                },
                "depth": {
                    "type": "integer",
                    "description": "External clone depth (default: 1)",
                    "default": 1,
                },
            },
            "required": ["branch_name", "clone_path"],
        },
        func=create_clone,
    )

    return registry


__all__ = ["create_clone_creation_registry"]
