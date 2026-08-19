"""Git-mutating clone MCP tools."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # exceptions from CloneGitManager's fixed git argv
from typing import Any, Literal

from gobby.clones import git as clone_git
from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.clones import Clone, CloneStatus
from gobby.storage.projects import LocalProjectManager
from gobby.utils.git import (
    get_checkout_mutation_lock,
    new_stash_marker,
    run_thread_to_completion,
    run_to_completion,
    stash_oid_for_marker,
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

    async def _delete_clone_impl(
        clone_id: str,
        force: bool = False,
        cancellation_requested: asyncio.Event | None = None,
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

        if cancellation_requested is not None and cancellation_requested.is_set():
            raise asyncio.CancelledError

        try:
            ctx.clone_storage.update(clone_id, status=CloneStatus.DELETING.value)
        except Exception as e:
            logger.exception("Failed to mark clone %s as deleting: %s", clone_id, e)
            return {"success": False, "error": f"Failed to mark clone deleting: {e}"}

        delete_error: str | None
        try:
            result = await asyncio.to_thread(
                git_manager.delete_clone,
                clone_path,
                force=force,
            )
        except Exception as error:
            delete_error = str(error)
        else:
            delete_error = None if result.success else result.error or result.message

        if delete_error is not None:
            logger.error("Failed to delete clone files for %s: %s", clone_id, delete_error)
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
                "error": f"Failed to delete clone files: {delete_error}",
            }

        try:
            ctx.clone_storage.delete(clone_id)
        except Exception as e:
            logger.error("Failed to delete clone record %s after file deletion: %s", clone_id, e)
            return {"success": False, "error": f"Failed to delete clone record: {e}"}

        return {"success": True, "message": f"Deleted clone {clone_id}"}

    async def adopt_clone_path(clone_path: str) -> Clone | dict[str, Any]:
        git_manager = ctx.git_manager
        if git_manager is None:
            return {"success": False, "error": "Clone tools require a git repository context"}
        if ctx.project_id is None:
            return {"success": False, "error": "No project context available for clone deletion"}

        project = LocalProjectManager(ctx.clone_storage.db).get(ctx.project_id)
        if project is None:
            return {"success": False, "error": f"Project not found: {ctx.project_id}"}

        resolved_path = git_manager.resolve_managed_clone_path(clone_path)
        if resolved_path is None:
            return {
                "success": False,
                "error": f"Clone path must be under {clone_git.CLONES_ROOT.expanduser()}",
            }

        project_directory = (clone_git.CLONES_ROOT.expanduser().resolve() / project.name).resolve()
        if resolved_path.parent != project_directory:
            return {
                "success": False,
                "error": f"Clone path must be directly under {project_directory}",
            }

        existing = ctx.clone_storage.get_by_path_any_status(str(resolved_path))
        if existing is not None:
            if existing.project_id != project.id:
                return {
                    "success": False,
                    "error": f"Clone path belongs to another project: {resolved_path}",
                }
            if existing.status != CloneStatus.CLEANUP.value:
                return existing

        if not resolved_path.is_dir():
            return {"success": False, "error": f"Clone path does not exist: {resolved_path}"}

        status = await asyncio.to_thread(git_manager.get_clone_status, resolved_path)
        if status is None or (status.branch is None and status.commit is None):
            return {"success": False, "error": f"Path is not a valid Git clone: {resolved_path}"}
        remote_url = await asyncio.to_thread(
            git_manager.get_remote_url,
            "origin",
            resolved_path,
        )
        base_branch = await asyncio.to_thread(git_manager.get_default_branch)

        try:
            clone, _ = ctx.clone_storage.register_adopted(
                project_id=project.id,
                branch_name=status.branch,
                clone_path=str(resolved_path),
                base_branch=base_branch,
                remote_url=remote_url,
            )
        except ValueError as error:
            return {"success": False, "error": str(error)}
        return clone

    async def delete_clone(
        clone_id: str | None = None,
        clone_path: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if (clone_id is None) == (clone_path is None):
            return {"success": False, "error": "Provide exactly one of clone_id or clone_path"}

        if clone_path is not None:
            adopted = await adopt_clone_path(clone_path)
            if isinstance(adopted, dict):
                return adopted
            clone_id = adopted.id

        assert clone_id is not None
        cancellation_requested = asyncio.Event()
        return await run_to_completion(
            _delete_clone_impl(clone_id, force, cancellation_requested),
            on_cancel=cancellation_requested.set,
        )

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
                "clone_path": {
                    "type": "string",
                    "description": "Managed clone path to adopt and delete",
                },
                "force": {
                    "type": "boolean",
                    "description": "Force deletion even with uncommitted changes",
                    "default": False,
                },
            },
            "oneOf": [
                {"required": ["clone_id"], "not": {"required": ["clone_path"]}},
                {"required": ["clone_path"], "not": {"required": ["clone_id"]}},
            ],
        },
        func=delete_clone,
    )

    async def _sync_clone_impl(
        clone_id: str,
        direction: Literal["pull", "push", "both"] = "pull",
        cancellation_requested: asyncio.Event | None = None,
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
        if clone.branch_name is None:
            return {"success": False, "error": f"Detached clone '{clone_id}' cannot be synced"}

        if cancellation_requested is not None and cancellation_requested.is_set():
            raise asyncio.CancelledError

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

    async def sync_clone(
        clone_id: str,
        direction: Literal["pull", "push", "both"] = "pull",
    ) -> dict[str, Any]:
        cancellation_requested = asyncio.Event()
        return await run_to_completion(
            _sync_clone_impl(clone_id, direction, cancellation_requested),
            on_cancel=cancellation_requested.set,
        )

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

    async def _merge_clone_impl(
        clone_id: str,
        target_branch: str = "main",
        cancellation_requested: asyncio.Event | None = None,
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
        if clone.branch_name is None:
            return {"success": False, "error": f"Detached clone '{clone_id}' cannot be merged"}

        # Step 1: Fetch clone's branch directly from clone path into main repo.
        # This avoids pushing to origin (which fails on divergent branches).
        ctx.clone_storage.mark_syncing(clone_id)
        temp_ref = f"clone-merge/{clone.branch_name}"
        mutation_lock = get_checkout_mutation_lock(git_manager.repo_path)
        merge_succeeded = False
        await mutation_lock.acquire()
        try:
            if cancellation_requested is not None and cancellation_requested.is_set():
                raise asyncio.CancelledError
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

            # Step 2: Stash dirty .gobby/ sync files to prevent merge conflicts.
            # Record the stash object created by this call so later stashes cannot
            # change which entry is restored.
            stash_oid: str | None = None
            warnings: list[str] = []
            stash_restore_error: str | None = None
            primary_result: dict[str, Any]
            try:
                ctx.clone_storage.record_sync(clone_id)
                try:
                    stash_marker = new_stash_marker("merge-clone")
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
                            stash_marker,
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
                        ["stash", "list", "--format=%H%x00%gs"],
                        cwd=git_manager.repo_path,
                        timeout=10,
                    )
                    if stash_head_after.returncode != 0:
                        raise subprocess.CalledProcessError(
                            stash_head_after.returncode,
                            ["git", "stash", "list", "--format=%H%x00%gs"],
                            output=stash_head_after.stdout,
                            stderr=stash_head_after.stderr,
                        )
                    before_oid = stash_head_before.stdout.strip() or None
                    after_oid = stash_head_after.stdout.partition("\0")[0].strip() or None
                    stash_oid = stash_oid_for_marker(stash_head_after.stdout, stash_marker)
                    if stash_oid is None and after_oid != before_oid:
                        raise subprocess.CalledProcessError(
                            1,
                            ["git", "stash", "list", "--format=%H%x00%gs"],
                            stderr=(
                                "stash head changed after push but the operation-owned "
                                "stash marker was not found"
                            ),
                        )
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
                            ctx.clone_storage.mark_merged(
                                clone_id,
                                cleanup_after=cleanup_after,
                            )
                            merge_succeeded = True
                            merge_sha = ""
                            sha_result = await run_thread_to_completion(
                                git_manager.run_git_command,
                                ["rev-parse", target_branch],
                                cwd=git_manager.repo_path,
                                timeout=10,
                            )
                            if sha_result.returncode == 0:
                                merge_sha = sha_result.stdout.strip()
                            primary_result = {
                                "success": True,
                                "message": (
                                    f"Successfully merged {clone.branch_name} into {target_branch}"
                                ),
                                "cleanup_after": cleanup_after,
                                "merge_sha": merge_sha,
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
                if not merge_succeeded:
                    ctx.clone_storage.update(
                        clone_id,
                        status=CloneStatus.ACTIVE.value,
                    )
            finally:
                mutation_lock.release()

    async def merge_clone(
        clone_id: str,
        target_branch: str = "main",
    ) -> dict[str, Any]:
        cancellation_requested = asyncio.Event()
        return await run_to_completion(
            _merge_clone_impl(
                clone_id,
                target_branch,
                cancellation_requested,
            ),
            on_cancel=cancellation_requested.set,
        )

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
