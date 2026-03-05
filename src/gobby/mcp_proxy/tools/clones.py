"""
Internal MCP tools for Gobby Clone Management.

Exposes functionality for:
- Creating git clones for isolated development
- Managing clone lifecycle (get, list, delete)
- Syncing clones with remote repositories

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.clones.git import CloneGitManager
    from gobby.storage.clones import LocalCloneManager

logger = logging.getLogger(__name__)


def create_clones_registry(
    clone_storage: LocalCloneManager,
    git_manager: CloneGitManager | None,
    project_id: str,
) -> InternalToolRegistry:
    """
    Create the gobby-clones MCP server registry.

    Args:
        clone_storage: Clone storage manager for CRUD operations
        git_manager: Git manager for clone operations (None when no git repo detected)
        project_id: Default project ID for new clones

    Returns:
        InternalToolRegistry with clone management tools
    """
    registry = InternalToolRegistry(
        name="gobby-clones",
        description="Git clone management for isolated development",
    )

    # ===== create_clone =====
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
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        try:
            if use_local:
                # Clone from local repo path — always full clone
                source = str(git_manager.repo_path)
                result = git_manager.full_clone(
                    remote_url=source,
                    clone_path=clone_path,
                    branch=branch_name,
                )
                if not remote_url:
                    remote_url = git_manager.get_remote_url() or source
            else:
                # Get remote URL if not provided
                if not remote_url:
                    remote_url = git_manager.get_remote_url()
                    if not remote_url:
                        return {
                            "success": False,
                            "error": "No remote URL provided and could not get from repository",
                        }

                # Create the clone
                result = git_manager.shallow_clone(
                    remote_url=remote_url,
                    clone_path=clone_path,
                    branch=branch_name,
                    depth=depth,
                )

            if not result.success:
                return {
                    "success": False,
                    "error": f"Clone failed: {result.error or result.message}",
                }

            # Store clone record
            clone = clone_storage.create(
                project_id=project_id,
                branch_name=branch_name,
                clone_path=clone_path,
                base_branch=base_branch,
                task_id=task_id,
                remote_url=remote_url,
            )

            return {
                "success": True,
                "clone": clone.to_dict(),
                "message": f"Created clone at {clone_path}",
            }

        except Exception as e:
            logger.error(f"Error creating clone: {e}")
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
                    "description": "Clone from local repo path instead of remote URL (full clone, preserves unpushed commits)",
                    "default": False,
                },
            },
            "required": ["branch_name", "clone_path"],
        },
        func=create_clone,
    )

    # ===== get_clone =====
    async def get_clone(clone_id: str) -> dict[str, Any]:
        """
        Get clone by ID.

        Args:
            clone_id: Clone ID

        Returns:
            Dict with clone info or error
        """
        clone = clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        return {"success": True, "clone": clone.to_dict()}

    registry.register(
        name="get_clone",
        description="Get clone by ID",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID",
                },
            },
            "required": ["clone_id"],
        },
        func=get_clone,
    )

    # ===== list_clones =====
    async def list_clones(
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        List clones with optional filters.

        Args:
            status: Filter by status (active, syncing, stale, cleanup)
            limit: Maximum number of results

        Returns:
            Dict with list of clones
        """
        clones = clone_storage.list_clones(
            project_id=project_id,
            status=status,
            limit=limit,
        )

        return {
            "success": True,
            "clones": [c.to_dict() for c in clones],
            "count": len(clones),
        }

    registry.register(
        name="list_clones",
        description="List clones with optional status filter",
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (active, syncing, stale, cleanup)",
                    "enum": ["active", "syncing", "stale", "cleanup"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results",
                    "default": 50,
                },
            },
        },
        func=list_clones,
    )

    # ===== delete_clone =====
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
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        clone = clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        # Store clone info for potential rollback
        clone_path = clone.clone_path

        # Delete the database record first (can be rolled back more easily)
        try:
            clone_storage.delete(clone_id)
        except Exception as e:
            logger.error(f"Failed to delete clone record {clone_id}: {e}")
            return {"success": False, "error": f"Failed to delete clone record: {e}"}

        # Delete the files
        result = git_manager.delete_clone(clone_path, force=force)
        if not result.success:
            # Rollback: recreate the clone record since file deletion failed
            logger.error(
                f"Failed to delete clone files for {clone_id}, "
                f"attempting to restore record: {result.error or result.message}"
            )
            try:
                clone_storage.create(
                    project_id=clone.project_id,
                    branch_name=clone.branch_name,
                    clone_path=clone_path,
                    base_branch=clone.base_branch,
                    task_id=clone.task_id,
                    remote_url=clone.remote_url,
                )
                logger.info(f"Restored clone record for {clone_id} after file deletion failure")
            except Exception as restore_error:
                logger.error(
                    f"Failed to restore clone record {clone_id}: {restore_error}. "
                    f"Clone is now orphaned in database."
                )
            return {
                "success": False,
                "error": f"Failed to delete clone files: {result.error or result.message}",
            }

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

    # ===== sync_clone =====
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
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        clone = clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        # Mark as syncing
        clone_storage.mark_syncing(clone_id)

        try:
            result = git_manager.sync_clone(
                clone_path=clone.clone_path,
                direction=direction,
            )

            if result.success:
                # Record successful sync and mark as active
                clone_storage.record_sync(clone_id)
                clone_storage.update(clone_id, status="active")
                return {"success": True, "message": f"Synced clone {clone_id} ({direction})"}
            else:
                return {
                    "success": False,
                    "error": f"Sync failed: {result.error or result.message}",
                }

        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            # Ensure status is reset to active if record_sync didn't complete
            clone = clone_storage.get(clone_id)
            if clone and clone.status == "syncing":
                clone_storage.update(clone_id, status="active")

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

    # ===== merge_clone =====
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
        if git_manager is None:
            return {
                "success": False,
                "error": "Clone tools require a git repository context",
            }

        from datetime import UTC, datetime, timedelta

        clone = clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        # Step 1: Push clone changes to remote
        clone_storage.mark_syncing(clone_id)
        sync_result = git_manager.sync_clone(
            clone_path=clone.clone_path,
            direction="push",
        )

        if not sync_result.success:
            clone_storage.update(clone_id, status="active")
            return {
                "success": False,
                "error": f"Sync failed: {sync_result.error or sync_result.message}",
                "step": "sync",
            }

        clone_storage.record_sync(clone_id)

        # Step 2: Merge in main repo
        merge_result = git_manager.merge_branch(
            source_branch=clone.branch_name,
            target_branch=target_branch,
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

        # Step 3: Success - set cleanup_after
        cleanup_after = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        clone_storage.update(clone_id, cleanup_after=cleanup_after)

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

    # ===== claim_clone =====
    async def claim_clone(
        clone_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Claim a clone for an agent session.

        Args:
            clone_id: Clone ID to claim
            session_id: Session ID claiming ownership

        Returns:
            Dict with success status
        """
        clone = clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        if clone.agent_session_id and clone.agent_session_id != session_id:
            return {
                "success": False,
                "error": f"Clone already claimed by session '{clone.agent_session_id}'",
            }

        updated = clone_storage.claim(clone_id, session_id)
        if not updated:
            return {"success": False, "error": "Failed to claim clone"}

        return {"success": True}

    registry.register(
        name="claim_clone",
        description="Claim ownership of a clone for an agent session",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID to claim",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID claiming ownership",
                },
            },
            "required": ["clone_id", "session_id"],
        },
        func=claim_clone,
    )

    # ===== release_clone =====
    async def release_clone(clone_id: str) -> dict[str, Any]:
        """
        Release a clone from its current owner.

        Args:
            clone_id: Clone ID to release

        Returns:
            Dict with success status
        """
        clone = clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        updated = clone_storage.release(clone_id)
        if not updated:
            return {"success": False, "error": "Failed to release clone"}

        return {"success": True}

    registry.register(
        name="release_clone",
        description="Release ownership of a clone",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID to release",
                },
            },
            "required": ["clone_id"],
        },
        func=release_clone,
    )

    # ===== get_clone_by_task =====
    async def get_clone_by_task(task_id: str) -> dict[str, Any]:
        """
        Get clone linked to a specific task.

        Args:
            task_id: Task ID to look up

        Returns:
            Dict with clone details or not found
        """
        clone = clone_storage.get_by_task(task_id)
        if not clone:
            return {"success": False, "error": f"No clone linked to task '{task_id}'"}

        return {"success": True, "clone": clone.to_dict()}

    registry.register(
        name="get_clone_by_task",
        description="Get clone linked to a specific task",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to look up",
                },
            },
            "required": ["task_id"],
        },
        func=get_clone_by_task,
    )

    # ===== link_task_to_clone =====
    async def link_task_to_clone(
        clone_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """
        Link a task to an existing clone.

        Args:
            clone_id: Clone ID
            task_id: Task ID to link

        Returns:
            Dict with success status
        """
        clone = clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        updated = clone_storage.update(clone_id, task_id=task_id)
        if not updated:
            return {"success": False, "error": "Failed to link task to clone"}

        return {"success": True}

    registry.register(
        name="link_task_to_clone",
        description="Link a task to an existing clone",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID to link",
                },
            },
            "required": ["clone_id", "task_id"],
        },
        func=link_task_to_clone,
    )

    # ===== get_clone_stats =====
    async def get_clone_stats() -> dict[str, Any]:
        """
        Get clone statistics for the project.

        Returns:
            Dict with counts by status
        """
        counts = clone_storage.count_by_status(project_id)

        return {
            "success": True,
            "project_id": project_id,
            "counts": counts,
            "total": sum(counts.values()),
        }

    registry.register(
        name="get_clone_stats",
        description="Get clone statistics (counts by status) for the project",
        input_schema={
            "type": "object",
            "properties": {},
        },
        func=get_clone_stats,
    )

    # ===== detect_stale_clones =====
    async def detect_stale_clones(
        hours: int | str = 24,
        limit: int | str = 50,
    ) -> dict[str, Any]:
        """
        Find clones with no activity for a period.

        Args:
            hours: Hours of inactivity threshold (default: 24)
            limit: Maximum results (default: 50)

        Returns:
            Dict with list of stale clones
        """
        hours = int(hours) if isinstance(hours, str) else hours
        limit = int(limit) if isinstance(limit, str) else limit

        stale = clone_storage.find_stale(
            project_id=project_id,
            hours=hours,
            limit=limit,
        )

        return {
            "success": True,
            "stale_clones": [
                {
                    "id": c.id,
                    "branch_name": c.branch_name,
                    "clone_path": c.clone_path,
                    "updated_at": c.updated_at,
                    "task_id": c.task_id,
                }
                for c in stale
            ],
            "count": len(stale),
            "threshold_hours": hours,
        }

    registry.register(
        name="detect_stale_clones",
        description="Find clones with no activity for a period",
        input_schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours of inactivity threshold",
                    "default": 24,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results",
                    "default": 50,
                },
            },
        },
        func=detect_stale_clones,
    )

    # ===== cleanup_stale_clones =====
    async def cleanup_stale_clones(
        hours: int | str = 24,
        dry_run: bool | str = True,
        delete_files: bool | str = False,
    ) -> dict[str, Any]:
        """
        Mark and optionally delete stale clones.

        Args:
            hours: Hours of inactivity threshold (default: 24)
            dry_run: If True, only report what would be cleaned (default: True)
            delete_files: If True, also delete clone files (default: False)

        Returns:
            Dict with cleanup results
        """
        hours = int(hours) if isinstance(hours, str) else hours
        dry_run = dry_run in (True, "true", "True", "1") if isinstance(dry_run, str) else dry_run
        delete_files = (
            delete_files in (True, "true", "True", "1")
            if isinstance(delete_files, str)
            else delete_files
        )

        stale = clone_storage.cleanup_stale(
            project_id=project_id,
            hours=hours,
            dry_run=dry_run,
        )

        results = []
        for c in stale:
            result_item: dict[str, Any] = {
                "id": c.id,
                "branch_name": c.branch_name,
                "clone_path": c.clone_path,
                "marked_stale": not dry_run,
                "files_deleted": False,
            }

            if delete_files and not dry_run and git_manager:
                git_result = git_manager.delete_clone(c.clone_path, force=True)
                result_item["files_deleted"] = git_result.success
                if not git_result.success:
                    result_item["delete_error"] = git_result.error or "Unknown error"

            results.append(result_item)

        return {
            "success": True,
            "dry_run": dry_run,
            "cleaned": results,
            "count": len(results),
            "threshold_hours": hours,
        }

    registry.register(
        name="cleanup_stale_clones",
        description="Mark and optionally delete stale clones",
        input_schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours of inactivity threshold",
                    "default": 24,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only report what would be cleaned",
                    "default": True,
                },
                "delete_files": {
                    "type": "boolean",
                    "description": "If true, also delete clone files on disk",
                    "default": False,
                },
            },
        },
        func=cleanup_stale_clones,
    )

    return registry
