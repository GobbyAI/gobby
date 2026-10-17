"""Git clone isolation handler."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from gobby.agents.cargo_target import cleanup_checkout_cargo_target_dir
from gobby.agents.isolation_models import (
    IsolationContext,
    IsolationHandler,
    SpawnConfig,
    SpawnStateKey,
    generate_branch_name,
    spawn_state_key,
)
from gobby.agents.isolation_repair import repair_isolation_environment
from gobby.storage.tasks import TaskArtifactManager
from gobby.worktrees.base_branch import select_worktree_base

logger = logging.getLogger("gobby.agents.isolation")


class CloneIsolationHandler(IsolationHandler):
    """
    Clone isolation - clone the local repository for full isolation.

    This handler:
    - Checks for existing clones by branch name
    - Creates new clones at the selected fork commit if needed
    - Adds an isolation context banner to the prompt
    """

    def __init__(
        self,
        clone_manager: Any,  # CloneGitManager
        clone_storage: Any,  # LocalCloneManager
        git_manager: Any | None = None,  # GitManager for branch detection
    ) -> None:
        """
        Initialize CloneIsolationHandler with dependencies.

        Args:
            clone_manager: Git manager for clone operations
            clone_storage: Storage for clone records
            git_manager: Git manager for source repo (optional, for branch detection)
        """
        self._clone_manager = clone_manager
        self._clone_storage = clone_storage
        self._git_manager = git_manager
        # Track partial state for cleanup on failure
        self._partial_clones: dict[SpawnStateKey, dict[str, str | None]] = {}

    async def prepare_environment(self, config: SpawnConfig) -> IsolationContext:
        """
        Prepare clone environment.

        - Select the fork commit once (omitted base: the caller's HEAD)
        - Generate branch name if not provided
        - Check for existing clone for the branch
        - Clone the local repository at the selected commit if needed
        - Return IsolationContext with clone info
        """
        if self._git_manager is None:
            raise RuntimeError("Clone isolation requires the source repository's git manager")
        base_branch, base_commit_sha = await select_worktree_base(
            self._git_manager, config.base_branch
        )

        # Reset partial state
        state_key = spawn_state_key(config)
        partial_state: dict[str, str | None] = {"path": None, "id": None}
        self._partial_clones[state_key] = partial_state

        branch_name = generate_branch_name(config)

        # Check if clone already exists for this branch
        existing = await asyncio.to_thread(
            self._clone_storage.get_by_branch, config.project_id, branch_name
        )
        if existing:
            if Path(existing.clone_path).is_dir():
                await repair_isolation_environment(
                    main_repo_path=config.project_path,
                    isolated_path=existing.clone_path,
                    provider=config.provider,
                    phase_timings_ms=config.phase_timings_ms,
                )
                # Use existing clone
                self._partial_clones.pop(state_key, None)
                return IsolationContext(
                    cwd=existing.clone_path,
                    branch_name=existing.branch_name,
                    clone_id=existing.id,
                    isolation_type="clone",
                    extra={
                        "source_repo": config.project_path,
                        "base_branch": existing.base_branch,
                    },
                )
            else:
                # Stale record — directory gone, clean up and fall through to create new

                logger.warning(
                    "Clone directory missing: %s (cleaning up stale record %s)",
                    existing.clone_path,
                    existing.id,
                )
                cargo_error = await asyncio.to_thread(
                    cleanup_checkout_cargo_target_dir,
                    Path(existing.clone_path),
                    existing.project_id,
                )
                if cargo_error is not None:
                    raise RuntimeError(
                        "cargo_target_cleanup_failed: Clone files were already absent, but "
                        f"Cargo target cleanup failed: {cargo_error}"
                    )
                await asyncio.to_thread(self._clone_storage.delete, existing.id)

        # Generate clone path
        project_name = Path(config.project_path).name
        clone_path = self._generate_clone_path(branch_name, project_name)

        # Clone the local repository at the selected commit
        result = await self._clone_manager.create_clone(
            clone_path=clone_path,
            branch_name=branch_name,
            base_branch=base_branch,
            base_commit_sha=base_commit_sha,
        )

        if not result.success:
            raise RuntimeError(f"Failed to create clone: {result.error}")

        # Track for cleanup — clone exists on disk now
        partial_state["path"] = clone_path

        # Record in storage
        clone = await asyncio.to_thread(
            self._clone_storage.create,
            project_id=config.project_id,
            branch_name=branch_name,
            clone_path=clone_path,
            base_branch=base_branch,
            task_id=config.task_id,
        )

        # Track storage record for cleanup
        partial_state["id"] = clone.id

        if config.task_id is not None:
            await asyncio.to_thread(
                TaskArtifactManager(self._clone_storage.db).set_artifacts_atomic,
                config.task_id,
                clone_path=clone_path,
                clone_id=clone.id,
                base_commit_sha=base_commit_sha,
            )

        await repair_isolation_environment(
            main_repo_path=config.project_path,
            isolated_path=clone_path,
            provider=config.provider,
            phase_timings_ms=config.phase_timings_ms,
        )

        # Success — clear partial state
        self._partial_clones.pop(state_key, None)

        return IsolationContext(
            cwd=clone.clone_path,
            branch_name=clone.branch_name,
            clone_id=clone.id,
            isolation_type="clone",
            extra={
                "source_repo": config.project_path,
                "base_branch": base_branch,
                "base_commit_sha": base_commit_sha,
            },
        )

    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """Clean up partially created clone on prepare failure."""
        partial_state = self._partial_clones.pop(spawn_state_key(config), None)
        if partial_state is None:
            logger.debug(
                "Skipping clone cleanup for %s: no partial clone state recorded",
                config.task_id or config.project_id,
            )
            return
        clone_path = partial_state.get("path")
        clone_id = partial_state.get("id")
        files_deleted = not clone_path or not Path(clone_path).exists()

        if clone_path:
            try:
                result = await self._clone_manager.delete_clone(
                    clone_path=clone_path,
                    force=True,
                )
                files_deleted = bool(result.success) or not Path(clone_path).exists()
                if files_deleted:
                    logger.info("Cleaned up partial clone: %s", clone_path)
                else:
                    logger.warning("Failed to clean up clone %s: %s", clone_path, result)
            except Exception as e:
                logger.warning("Failed to clean up clone %s: %s", clone_path, e)

        record_terminal = clone_id is None
        if clone_id and files_deleted:
            try:
                terminal = await asyncio.to_thread(self._clone_storage.mark_cleanup, clone_id)
                record_terminal = terminal is not None
            except Exception as e:
                logger.warning("Failed to mark partial clone %s for cleanup: %s", clone_id, e)

        cargo_error = None
        if clone_path and files_deleted:
            cargo_error = await asyncio.to_thread(
                cleanup_checkout_cargo_target_dir,
                Path(clone_path),
                config.project_id,
            )
            if cargo_error is not None:
                logger.warning(
                    "cargo_target_cleanup_failed for deleted clone %s: %s",
                    clone_path,
                    cargo_error,
                )

        if clone_id and files_deleted and cargo_error is None and record_terminal:
            try:
                await asyncio.to_thread(self._clone_storage.delete, clone_id)
                logger.info("Cleaned up clone storage record: %s", clone_id)
            except Exception as e:
                logger.warning("Failed to clean up clone record %s: %s", clone_id, e)

    def build_context_prompt(self, original_prompt: str, ctx: IsolationContext) -> str:
        """
        Build prompt with the clone context banner.

        Prepends isolation context to help the agent understand it's
        working in a clone, not the original repository.
        """
        warning = (
            "Clone context — you are working in an isolated clone, "
            "not the original repository.\n"
            f"- Branch: {ctx.branch_name}\n"
            f"- Clone path: {ctx.cwd}\n"
            f"- Source repo: {ctx.extra.get('source_repo', 'unknown')}\n"
            "\n"
            "Changes in this clone are fully isolated from the original repository.\n"
            "Push your changes when ready to share with the original.\n"
            "\n"
            "---\n"
            "\n"
        )
        return warning + original_prompt

    def _generate_clone_path(self, branch_name: str, project_name: str) -> str:
        """Generate a unique clone path in ~/.gobby/clones/."""
        safe_branch = branch_name.replace("/", "-").replace("\\", "-")
        return str(
            Path.home() / ".gobby" / "clones" / project_name / f"{safe_branch}-{uuid4().hex[:8]}"
        )
