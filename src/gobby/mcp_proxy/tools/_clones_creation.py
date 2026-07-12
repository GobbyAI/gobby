"""Clone creation MCP tools."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

logger = logging.getLogger(__name__)


def create_clone_creation_registry(ctx: CloneRegistryContext) -> InternalToolRegistry:
    """Create a registry with clone creation tools."""
    registry = InternalToolRegistry(
        name="gobby-clones-creation",
        description="Clone creation tools",
    )

    async def create_clone(
        branch_name: str,
        clone_path: str,
        remote_url: str | None = None,
        task_id: str | None = None,
        base_branch: str = "main",
        depth: int = 1,
        use_local: bool = False,
    ) -> dict[str, Any]:
        """
        Create a new git clone.

        Args:
            branch_name: Branch to clone
            clone_path: Path where clone will be created
            remote_url: Remote URL (defaults to origin of parent repo)
            task_id: Optional task ID to link
            base_branch: Base branch for the clone
            depth: Clone depth (default: 1 for shallow)
            use_local: If True, clone from local repo path instead of remote URL.
                       Forces full clone to preserve unpushed commits.

        Returns:
            Dict with clone info or error
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
        try:
            # Resolve task references before creating anything on disk. A bad task
            # reference must not leave a clone directory behind.
            resolved_task_id = ctx.resolve_task_id(task_id) if task_id else None

            if use_local:
                # Clone from local repo path - always full clone
                # Clone base_branch first, then create branch_name as new branch
                source = str(git_manager.repo_path)
                result = await asyncio.to_thread(
                    git_manager.full_clone,
                    remote_url=source,
                    clone_path=clone_path,
                    branch=base_branch,
                )
                clone_created = result.success
                if result.success and branch_name != base_branch:
                    await asyncio.to_thread(
                        git_manager.run_git_command,
                        ["checkout", "-b", branch_name],
                        cwd=clone_path,
                        check=True,
                    )
                if not remote_url:
                    remote_url = await asyncio.to_thread(git_manager.get_remote_url) or source
            else:
                # Get remote URL if not provided
                if not remote_url:
                    remote_url = await asyncio.to_thread(git_manager.get_remote_url)
                    if not remote_url:
                        return {
                            "success": False,
                            "error": "No remote URL provided and could not get from repository",
                        }

                # Create the clone
                result = await asyncio.to_thread(
                    git_manager.shallow_clone,
                    remote_url=remote_url,
                    clone_path=clone_path,
                    branch=branch_name,
                    depth=depth,
                )
                clone_created = result.success

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
                base_branch=base_branch,
                task_id=resolved_task_id,
                remote_url=remote_url,
            )
            record_created = True

            return {
                "success": True,
                "clone": clone.to_dict(),
                "message": f"Created clone at {clone_path}",
            }

        except Exception as e:
            if clone_created and not record_created:
                try:
                    cleanup_result = await asyncio.to_thread(
                        git_manager.delete_clone,
                        clone_path,
                        force=True,
                    )
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
            logger.error("Error creating clone: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    registry.register(
        name="create_clone",
        description="Create a new git clone for isolated development",
        input_schema={
            "type": "object",
            "properties": {
                "branch_name": {
                    "type": "string",
                    "description": "Branch to clone",
                },
                "clone_path": {
                    "type": "string",
                    "description": "Path where clone will be created",
                },
                "remote_url": {
                    "type": "string",
                    "description": "Remote URL (defaults to origin of parent repo)",
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional task ID to link",
                },
                "base_branch": {
                    "type": "string",
                    "description": "Base branch for the clone",
                    "default": "main",
                },
                "depth": {
                    "type": "integer",
                    "description": "Clone depth (default: 1 for shallow)",
                    "default": 1,
                },
                "use_local": {
                    "type": "boolean",
                    "description": (
                        "Clone from local repo path instead of remote URL "
                        "(full clone, preserves unpushed commits)"
                    ),
                    "default": False,
                },
            },
            "required": ["branch_name", "clone_path"],
        },
        func=create_clone,
    )

    return registry


__all__ = ["create_clone_creation_registry"]
