"""Git-mutating clone MCP tools."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # exceptions from CloneGitManager's fixed git argv
from typing import Any, Literal

from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.clones import CloneStatus
from gobby.utils.git import (
    get_checkout_mutation_lock,
    run_thread_to_completion,
    stash_ref_for_oid,
)

logger = logging.getLogger(__name__)


def _git_exception_result(
    step: str,
    error: subprocess.TimeoutExpired | OSError,
) -> dict[str, Any]:
    """Convert an expected git execution exception into a tool result."""
    if isinstance(error, subprocess.TimeoutExpired):
        message = f"{step.capitalize()} timed out after {error.timeout} seconds"
    else:
        message = f"{step.capitalize()} failed: {error}"
    return {"success": False, "error": message, "step": step}


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

        result = await asyncio.to_thread(
            git_manager.delete_clone,
            clone_path,
            force=force,
        )
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
            result = await asyncio.to_thread(
                git_manager.sync_clone,
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
        mutation_lock = get_checkout_mutation_lock(git_manager.repo_path)
        await mutation_lock.acquire()
        try:
            try:
                fetch_result = await run_thread_to_completion(
                    git_manager.run_git_command,
                    [
                        "fetch",
                        str(clone.clone_path),
                        f"{clone.branch_name}:refs/heads/{temp_ref}",
                    ],
                    cwd=git_manager.repo_path,
                    timeout=120,
                )
            except (subprocess.TimeoutExpired, OSError) as error:
                return _git_exception_result("fetch", error)

            if fetch_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Fetch from clone failed: {fetch_result.stderr}",
                    "step": "fetch",
                }

            ctx.clone_storage.record_sync(clone_id)

            # Step 2: Stash dirty .gobby/ sync files to prevent merge conflicts.
            # Record the stash object created by this call so later stashes cannot
            # change which entry is restored.
            stash_oid: str | None = None
            warnings: list[str] = []
            stash_restore_error: str | None = None
            primary_result: dict[str, Any]
            try:
                try:
                    stash_head_before = await run_thread_to_completion(
                        git_manager.run_git_command,
                        ["stash", "list", "-1", "--format=%H"],
                        cwd=git_manager.repo_path,
                        timeout=10,
                    )
                    if stash_head_before.returncode != 0:
                        raise subprocess.CalledProcessError(
                            stash_head_before.returncode,
                            ["git", "stash", "list", "-1", "--format=%H"],
                            output=stash_head_before.stdout,
                            stderr=stash_head_before.stderr,
                        )
                    stash_result = await run_thread_to_completion(
                        git_manager.run_git_command,
                        [
                            "stash",
                            "push",
                            "-m",
                            "gobby-merge-clone: auto-stash sync files",
                            "--",
                            ".gobby/",
                        ],
                        cwd=git_manager.repo_path,
                        timeout=10,
                    )
                    if stash_result.returncode != 0:
                        raise subprocess.CalledProcessError(
                            stash_result.returncode,
                            ["git", "stash", "push", "--", ".gobby/"],
                            output=stash_result.stdout,
                            stderr=stash_result.stderr,
                        )
                    stash_head_after = await run_thread_to_completion(
                        git_manager.run_git_command,
                        ["stash", "list", "-1", "--format=%H"],
                        cwd=git_manager.repo_path,
                        timeout=10,
                    )
                    if stash_head_after.returncode != 0:
                        raise subprocess.CalledProcessError(
                            stash_head_after.returncode,
                            ["git", "stash", "list", "-1", "--format=%H"],
                            output=stash_head_after.stdout,
                            stderr=stash_head_after.stderr,
                        )
                    before_oid = stash_head_before.stdout.strip() or None
                    after_oid = stash_head_after.stdout.strip() or None
                    if after_oid != before_oid:
                        if after_oid is None:
                            raise subprocess.CalledProcessError(
                                1,
                                ["git", "stash", "list", "-1", "--format=%H"],
                                stderr="stash identity disappeared after stash push",
                            )
                        stash_oid = after_oid
                except subprocess.CalledProcessError as error:
                    detail = (
                        error.stderr or error.output or f"git exited with status {error.returncode}"
                    )
                    primary_result = {
                        "success": False,
                        "error": f"Stash failed: {detail}",
                        "step": "stash",
                    }
                except (subprocess.TimeoutExpired, OSError) as error:
                    primary_result = _git_exception_result("stash", error)
                else:
                    # Step 3: Merge the fetched ref into target branch.
                    try:
                        merge_result = await run_thread_to_completion(
                            git_manager.merge_branch,
                            source_branch=temp_ref,
                            target_branch=target_branch,
                            source_is_local=True,
                        )
                    except (subprocess.TimeoutExpired, OSError) as error:
                        primary_result = _git_exception_result("merge", error)
                    else:
                        if not merge_result.success:
                            if merge_result.error == "merge_conflict":
                                conflicted_files = (
                                    merge_result.output.split("\n") if merge_result.output else []
                                )
                                primary_result = {
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
                            else:
                                primary_result = {
                                    "success": False,
                                    "has_conflicts": False,
                                    "error": merge_result.error or merge_result.message,
                                    "step": "merge",
                                }
                        else:
                            cleanup_after = (datetime.now(UTC) + timedelta(days=7)).isoformat()
                            ctx.clone_storage.update(clone_id, cleanup_after=cleanup_after)
                            primary_result = {
                                "success": True,
                                "message": (
                                    f"Successfully merged {clone.branch_name} into {target_branch}"
                                ),
                                "cleanup_after": cleanup_after,
                            }
            finally:
                try:
                    delete_result = await run_thread_to_completion(
                        git_manager.run_git_command,
                        ["branch", "-D", temp_ref],
                        cwd=git_manager.repo_path,
                        timeout=10,
                    )
                    if delete_result.returncode != 0:
                        detail = (
                            delete_result.stderr
                            or delete_result.stdout
                            or f"git exited with status {delete_result.returncode}"
                        )
                        warnings.append(f"Failed to delete temporary branch {temp_ref}: {detail}")
                except (subprocess.TimeoutExpired, OSError) as error:
                    warnings.append(f"Failed to delete temporary branch {temp_ref}: {error}")

                if stash_oid:
                    try:
                        stash_list_result = await run_thread_to_completion(
                            git_manager.run_git_command,
                            ["stash", "list", "--format=%gd%x00%H"],
                            cwd=git_manager.repo_path,
                            timeout=10,
                        )
                        if stash_list_result.returncode != 0:
                            detail = (
                                stash_list_result.stderr
                                or stash_list_result.stdout
                                or f"git exited with status {stash_list_result.returncode}"
                            )
                            raise subprocess.CalledProcessError(
                                stash_list_result.returncode,
                                ["git", "stash", "list"],
                                output=stash_list_result.stdout,
                                stderr=detail,
                            )
                        stash_ref = stash_ref_for_oid(stash_list_result.stdout, stash_oid)
                        if stash_ref is None:
                            raise RuntimeError(f"exact stash {stash_oid} is no longer present")
                        pop_result = await run_thread_to_completion(
                            git_manager.run_git_command,
                            ["stash", "pop", stash_ref],
                            cwd=git_manager.repo_path,
                            timeout=10,
                        )
                        if pop_result.returncode != 0:
                            detail = (
                                pop_result.stderr
                                or pop_result.stdout
                                or f"git exited with status {pop_result.returncode}"
                            )
                            stash_restore_error = (
                                f"Failed to restore stashed .gobby/ files: {detail}"
                            )
                    except (
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                        OSError,
                        RuntimeError,
                    ) as error:
                        stash_restore_error = f"Failed to restore stashed .gobby/ files: {error}"

                    if stash_restore_error:
                        warnings.append(stash_restore_error)

            for warning in warnings:
                logger.warning(warning)
            if warnings:
                primary_result["warnings"] = warnings
            if stash_restore_error:
                primary_result["stash_restore_error"] = stash_restore_error
                if primary_result.get("success") is True:
                    primary_result["success"] = False
                    primary_result["error"] = stash_restore_error
                    primary_result["step"] = "stash_restore"
            return primary_result
        finally:
            try:
                ctx.clone_storage.update(
                    clone_id,
                    status=CloneStatus.ACTIVE.value,
                )
            finally:
                mutation_lock.release()

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
