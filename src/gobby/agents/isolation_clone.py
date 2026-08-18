"""Git clone isolation handler."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # needed for git error handling
from pathlib import Path
from typing import Any
from uuid import uuid4

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

logger = logging.getLogger("gobby.agents.isolation")


def _capture_base_commit_sha(isolation_path: str) -> str:
    result = subprocess.run(  # nosec B603 B607 # fixed git argv on local isolation path.
        ["git", "-C", isolation_path, "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git rev-parse HEAD failed"
        raise RuntimeError(f"Failed to capture base_commit_sha for {isolation_path}: {detail}")
    return result.stdout.strip()


class CloneIsolationHandler(IsolationHandler):
    """
    Clone isolation - create a shallow clone for full isolation.

    This handler:
    - Checks for existing clones by branch name
    - Creates new shallow clones if needed
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

        - Generate branch name if not provided
        - Check for existing clone for the branch
        - Create new shallow clone if needed
        - Return IsolationContext with clone info
        """
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
                )
                # Use existing clone
                self._partial_clones.pop(state_key, None)
                return IsolationContext(
                    cwd=existing.clone_path,
                    branch_name=existing.branch_name,
                    clone_id=existing.id,
                    isolation_type="clone",
                    extra={"source_repo": config.project_path},
                )
            else:
                # Stale record — directory gone, clean up and fall through to create new

                logger.warning(
                    "Clone directory missing: %s (cleaning up stale record %s)",
                    existing.clone_path,
                    existing.id,
                )
                await asyncio.to_thread(self._clone_storage.delete, existing.id)

        # Determine base branch - use parent's current branch if default "main" was passed
        base_branch = config.base_branch
        use_local = False

        # If base_branch is the default "main", check if parent is on a different branch
        if self._git_manager is not None:
            current_branch = await asyncio.to_thread(self._git_manager.get_current_branch)
            if current_branch and base_branch == "main" and current_branch != "main":
                # Use parent's current branch instead
                base_branch = current_branch

                logger.info("Using parent's current branch '%s' for clone", base_branch)

            # Check for unpushed commits on the base branch
            try:
                has_unpushed, unpushed_count = await asyncio.to_thread(
                    self._git_manager.has_unpushed_commits, base_branch
                )
                if has_unpushed:
                    use_local = True
                    logger.info(
                        "Using local repo for clone (%s unpushed commits on '%s')",
                        unpushed_count,
                        base_branch,
                    )
            except (subprocess.CalledProcessError, OSError):
                logger.warning(
                    "Failed to check unpushed commits for clone, using remote",
                    exc_info=True,
                )

        # Generate clone path
        project_name = Path(config.project_path).name
        clone_path = self._generate_clone_path(branch_name, project_name)

        # Create clone (full when use_local, shallow otherwise)
        result = await asyncio.to_thread(
            self._clone_manager.create_clone,
            clone_path=clone_path,
            branch_name=branch_name,
            base_branch=base_branch,
            shallow=not use_local,
            use_local=use_local,
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

        base_commit_sha: str | None = None
        if config.task_id is not None:
            base_commit_sha = await asyncio.to_thread(_capture_base_commit_sha, clone_path)
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
                **({"base_commit_sha": base_commit_sha} if base_commit_sha else {}),
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

        if clone_path:
            try:
                await asyncio.to_thread(
                    self._clone_manager.delete_clone,
                    clone_path=clone_path,
                    force=True,
                )
                logger.info("Cleaned up partial clone: %s", clone_path)
            except Exception as e:
                logger.warning("Failed to clean up clone %s: %s", clone_path, e)

        if clone_id:
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
        warning = f"""Clone context — you are working in an isolated shallow clone, not the original repository.
- Branch: {ctx.branch_name}
- Clone path: {ctx.cwd}
- Source repo: {ctx.extra.get("source_repo", "unknown")}

Changes in this clone are fully isolated from the original repository.
Push your changes when ready to share with the original.

---

"""
        return warning + original_prompt

    def _generate_clone_path(self, branch_name: str, project_name: str) -> str:
        """Generate a unique clone path in ~/.gobby/clones/."""
        safe_branch = branch_name.replace("/", "-").replace("\\", "-")
        return str(
            Path.home() / ".gobby" / "clones" / project_name / f"{safe_branch}-{uuid4().hex[:8]}"
        )
