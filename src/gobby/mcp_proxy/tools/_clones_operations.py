"""Git-mutating clone MCP tools."""

from __future__ import annotations

import logging
from typing import Any, Literal

from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.clones import CloneStatus

logger = logging.getLogger(__name__)


def create_clone_operations_registry(ctx: CloneRegistryContext) -> InternalToolRegistry:
    """Create a registry with clone mutation and sync tools."""
    registry = InternalToolRegistry(
        name="gobby-clones-operations",
        description="Clone mutation, sync, and merge tools",
    )

    async def delete_clone(
        clone_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Delete a clone.

        Args:
            clone_id: Clone ID to delete
            force: Force deletion even if there are uncommitted changes

        Returns:
            Dict with success status
        """
        git_manager = ctx.git_manager
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        clone = ctx.clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        clone_path = clone.clone_path
        previous_status = clone.status

        try:
            ctx.clone_storage.update(clone_id, status=CloneStatus.DELETING.value)
        except Exception as e:
            logger.error("Failed to mark clone %s as deleting: %s", clone_id, e, exc_info=True)
            return {"success": False, "error": f"Failed to mark clone deleting: {e}"}

        result = git_manager.delete_clone(clone_path, force=force)
        if not result.success:
            logger.error(
                f"Failed to delete clone files for {clone_id}: {result.error or result.message}"
            )
            try:
                ctx.clone_storage.update(clone_id, status=previous_status)
            except Exception:
                logger.warning(
                    "Failed to restore clone %s status after file deletion failure",
                    clone_id,
                    exc_info=True,
                )
            return {
                "success": False,
                "error": f"Failed to delete clone files: {result.error or result.message}",
            }

        try:
            ctx.clone_storage.delete(clone_id)
        except Exception as e:
            logger.error("Failed to delete clone record %s after file deletion: %s", clone_id, e)
            return {"success": False, "error": f"Failed to delete clone record: {e}"}

        return {"success": True, "message": f"Deleted clone {clone_id}"}

    registry.register(
        name="delete_clone",
        description="Delete a clone and its files",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID to delete",
                },
                "force": {
                    "type": "boolean",
                    "description": "Force deletion even with uncommitted changes",
                    "default": False,
                },
            },
            "required": ["clone_id"],
        },
        func=delete_clone,
    )

    async def sync_clone(
        clone_id: str,
        direction: Literal["pull", "push", "both"] = "pull",
    ) -> dict[str, Any]:
        """
        Sync a clone with its remote.

        Args:
            clone_id: Clone ID to sync
            direction: Sync direction (pull, push, or both)

        Returns:
            Dict with sync result
        """
        git_manager = ctx.git_manager
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        clone = ctx.clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        # Mark as syncing
        ctx.clone_storage.mark_syncing(clone_id)

        try:
            result = git_manager.sync_clone(
                clone_path=clone.clone_path,
                direction=direction,
            )

            if result.success:
                # Record successful sync and mark as active
                ctx.clone_storage.record_sync(clone_id)
                ctx.clone_storage.update(clone_id, status="active")
                return {"success": True, "message": f"Synced clone {clone_id} ({direction})"}
            return {
                "success": False,
                "error": f"Sync failed: {result.error or result.message}",
            }

        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            # Ensure status is reset to active if record_sync didn't complete
            clone = ctx.clone_storage.get(clone_id)
            if clone and clone.status == "syncing":
                ctx.clone_storage.update(clone_id, status="active")

    registry.register(
        name="sync_clone",
        description="Sync a clone with its remote repository",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID to sync",
                },
                "direction": {
                    "type": "string",
                    "description": "Sync direction",
                    "enum": ["pull", "push", "both"],
                    "default": "pull",
                },
            },
            "required": ["clone_id"],
        },
        func=sync_clone,
    )

    async def merge_clone(
        clone_id: str,
        target_branch: str = "main",
    ) -> dict[str, Any]:
        """
        Merge clone branch to target branch in main repository.

        Performs:
        1. Push clone changes to remote (sync_clone push)
        2. Fetch branch in main repo
        3. Attempt merge to target branch

        On success, sets cleanup_after to 7 days from now.

        Args:
            clone_id: Clone ID to merge
            target_branch: Target branch to merge into (default: main)

        Returns:
            Dict with merge result and conflict info if any
        """
        git_manager = ctx.git_manager
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        from datetime import UTC, datetime, timedelta

        clone = ctx.clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        # Step 1: Fetch clone's branch directly from clone path into main repo.
        # This avoids pushing to origin (which fails on divergent branches).
        ctx.clone_storage.mark_syncing(clone_id)
        temp_ref = f"clone-merge/{clone.branch_name}"
        fetch_result = git_manager.run_git_command(
            ["fetch", str(clone.clone_path), f"{clone.branch_name}:refs/heads/{temp_ref}"],
            cwd=git_manager.repo_path,
            timeout=120,
        )

        if fetch_result.returncode != 0:
            ctx.clone_storage.update(clone_id, status="active")
            return {
                "success": False,
                "error": f"Fetch from clone failed: {fetch_result.stderr}",
                "step": "fetch",
            }

        ctx.clone_storage.record_sync(clone_id)

        # Step 2: Stash dirty .gobby/ sync files to prevent merge conflicts.
        # Compare stash list before/after to reliably detect if a stash was created.
        stash_created = False
        stash_list_before = git_manager.run_git_command(
            ["stash", "list"],
            cwd=git_manager.repo_path,
            timeout=10,
        )
        stash_result = git_manager.run_git_command(
            ["stash", "push", "-m", "gobby-merge-clone: auto-stash sync files", "--", ".gobby/"],
            cwd=git_manager.repo_path,
            timeout=10,
        )
        if stash_result.returncode == 0:
            stash_list_after = git_manager.run_git_command(
                ["stash", "list"],
                cwd=git_manager.repo_path,
                timeout=10,
            )
            stash_created = stash_list_after.stdout != stash_list_before.stdout

        # Step 3: Merge the fetched ref into target branch
        try:
            merge_result = git_manager.merge_branch(
                source_branch=temp_ref,
                target_branch=target_branch,
                source_is_local=True,
            )
        finally:
            # Clean up temp ref regardless of merge outcome
            git_manager.run_git_command(
                ["branch", "-D", temp_ref],
                cwd=git_manager.repo_path,
                timeout=10,
            )
            # Restore stashed .gobby/ files
            if stash_created:
                pop_result = git_manager.run_git_command(
                    ["stash", "pop"],
                    cwd=git_manager.repo_path,
                    timeout=10,
                )
                if pop_result.returncode != 0:
                    logger.warning(
                        "Failed to restore stashed .gobby/ files: %s",
                        pop_result.stderr or pop_result.stdout,
                    )

        if not merge_result.success:
            # Check for conflicts
            if merge_result.error == "merge_conflict":
                conflicted_files = merge_result.output.split("\n") if merge_result.output else []
                return {
                    "success": False,
                    "has_conflicts": True,
                    "conflicted_files": conflicted_files,
                    "error": merge_result.message,
                    "step": "merge",
                    "message": (
                        f"Merge conflicts detected in {len(conflicted_files)} files. "
                        "Use gobby-merge tools to resolve."
                    ),
                }

            return {
                "success": False,
                "has_conflicts": False,
                "error": merge_result.error or merge_result.message,
                "step": "merge",
            }

        cleanup_after = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        ctx.clone_storage.update(clone_id, cleanup_after=cleanup_after)

        return {
            "success": True,
            "message": f"Successfully merged {clone.branch_name} into {target_branch}",
            "cleanup_after": cleanup_after,
        }

    registry.register(
        name="merge_clone",
        description="Merge clone branch to target branch in main repository",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID to merge",
                },
                "target_branch": {
                    "type": "string",
                    "description": "Target branch to merge into",
                    "default": "main",
                },
            },
            "required": ["clone_id"],
        },
        func=merge_clone,
    )

    return registry


__all__ = ["create_clone_operations_registry"]
