"""Git worktree isolation handler."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from gobby.agents import worktree_reuse
from gobby.agents.isolation_models import (
    IsolationContext,
    IsolationHandler,
    SpawnConfig,
    generate_branch_name,
)
from gobby.agents.isolation_repair import repair_isolation_environment
from gobby.storage.tasks import TaskArtifactManager

logger = logging.getLogger("gobby.agents.isolation")


class WorktreeIsolationHandler(IsolationHandler):
    """
    Worktree isolation - create/reuse a git worktree for isolated work.

    This handler:
    - Checks for existing worktrees by branch name
    - Creates new worktrees if needed
    - Copies project.json and installs hooks
    - Adds CRITICAL context warning to prompt
    """

    def __init__(
        self,
        git_manager: Any,  # WorktreeGitManager
        worktree_storage: Any,  # LocalWorktreeManager
    ) -> None:
        """
        Initialize WorktreeIsolationHandler with dependencies.

        Args:
            git_manager: Git manager for worktree operations
            worktree_storage: Storage for worktree records
        """
        self._git_manager = git_manager
        self._worktree_storage = worktree_storage
        # Track partial state for cleanup on failure
        self._created_worktree_path: str | None = None
        self._created_worktree_id: str | None = None

    async def prepare_environment(self, config: SpawnConfig) -> IsolationContext:
        """
        Prepare worktree environment.

        Prepare or reuse a git worktree and return isolation metadata.
        """
        # Reset partial state
        self._created_worktree_path = None
        self._created_worktree_id = None

        branch_name = generate_branch_name(config)
        base_branch = config.base_branch
        current_branch = await asyncio.to_thread(self._git_manager.get_current_branch)
        if current_branch and base_branch == "main" and current_branch != "main":
            base_branch = current_branch

        # Check if worktree already exists for this branch
        existing = await asyncio.to_thread(
            self._worktree_storage.get_by_branch, config.project_id, branch_name
        )
        if existing:
            if Path(existing.worktree_path).is_dir():
                live_claim = await asyncio.to_thread(
                    self._worktree_storage.is_claimed_by_live_session, existing.id
                )
                if live_claim:
                    raise RuntimeError(f"Cannot reuse claimed live worktree: {existing.id}")
                sync_result = await worktree_reuse.sync_reused_worktree_to_base(
                    git_manager=self._git_manager,
                    worktree_path=existing.worktree_path,
                    base_branch=base_branch,
                )
                await repair_isolation_environment(
                    main_repo_path=str(self._git_manager.repo_path),
                    isolated_path=existing.worktree_path,
                    provider=config.provider,
                )
                extra = {"main_repo_path": str(self._git_manager.repo_path)}
                existing_base_commit_sha = getattr(sync_result, "base_commit_sha", None)
                if isinstance(existing_base_commit_sha, str) and existing_base_commit_sha:
                    extra["base_commit_sha"] = existing_base_commit_sha
                # Use existing worktree
                return IsolationContext(
                    cwd=existing.worktree_path,
                    branch_name=existing.branch_name,
                    worktree_id=existing.id,
                    isolation_type="worktree",
                    extra=extra,
                )
            else:
                # Stale record — directory gone, clean up and fall through to create new
                logger.warning(
                    f"Worktree directory missing: {existing.worktree_path} "
                    f"(cleaning up stale record {existing.id})",
                )
                await asyncio.to_thread(
                    worktree_reuse.cleanup_stale_worktree_registration,
                    self._git_manager,
                    self._worktree_storage,
                    existing,
                )

        use_local = False

        # Check for unpushed commits on the base branch
        has_unpushed, unpushed_count = await asyncio.to_thread(
            self._git_manager.has_unpushed_commits, base_branch
        )
        if has_unpushed:
            # Use local branch ref to preserve unpushed commits
            use_local = True

            logger.info(
                f"Using local branch '{base_branch}' for worktree "
                f"({unpushed_count} unpushed commits)"
            )

        # Generate worktree path
        project_name = Path(self._git_manager.repo_path).name
        worktree_path = self._generate_worktree_path(branch_name, project_name)

        # Create git worktree
        result = await asyncio.to_thread(
            self._git_manager.create_worktree,
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_branch=base_branch,
            create_branch=True,
            use_local=use_local,
        )

        if not result.success:
            raise RuntimeError(f"Failed to create worktree: {result.error}")

        # Track for cleanup — worktree exists on disk now
        self._created_worktree_path = worktree_path

        # Record in storage
        worktree = await asyncio.to_thread(
            self._worktree_storage.create,
            project_id=config.project_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            base_branch=base_branch,
            task_id=config.task_id,
        )

        # Track storage record for cleanup
        self._created_worktree_id = worktree.id

        created_base_commit_sha: str | None = None
        if config.task_id is not None:
            created_base_commit_sha = await asyncio.to_thread(
                worktree_reuse.capture_worktree_base_commit_sha,
                git_manager=self._git_manager,
                worktree_path=worktree_path,
                base_branch=base_branch,
                use_local=use_local,
            )
            await asyncio.to_thread(
                TaskArtifactManager(self._worktree_storage.db).set_artifacts_atomic,
                config.task_id,
                worktree_path=worktree_path,
                worktree_id=worktree.id,
                base_commit_sha=created_base_commit_sha,
            )

        await repair_isolation_environment(
            main_repo_path=str(self._git_manager.repo_path),
            isolated_path=worktree_path,
            provider=config.provider,
        )

        # Success — clear partial state
        self._created_worktree_path = None
        self._created_worktree_id = None

        return IsolationContext(
            cwd=worktree.worktree_path,
            branch_name=worktree.branch_name,
            worktree_id=worktree.id,
            isolation_type="worktree",
            extra={
                "main_repo_path": str(self._git_manager.repo_path),
                **({"base_commit_sha": created_base_commit_sha} if created_base_commit_sha else {}),
            },
        )

    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """Clean up partially created worktree on prepare failure."""

        if self._created_worktree_path:
            try:
                await asyncio.to_thread(
                    self._git_manager.delete_worktree,
                    worktree_path=self._created_worktree_path,
                    force=True,
                )
                logger.info(f"Cleaned up partial worktree: {self._created_worktree_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up worktree {self._created_worktree_path}: {e}")

        if self._created_worktree_id:
            try:
                self._worktree_storage.delete(self._created_worktree_id)
                logger.info(f"Cleaned up worktree storage record: {self._created_worktree_id}")
            except Exception as e:
                logger.warning(
                    f"Failed to clean up worktree record {self._created_worktree_id}: {e}"
                )

        self._created_worktree_path = None
        self._created_worktree_id = None

    def build_context_prompt(self, original_prompt: str, ctx: IsolationContext) -> str:
        """
        Build prompt with CRITICAL worktree context warning.

        Prepends isolation context to help the agent understand it's
        working in a worktree, not the main repository.
        """
        warning = f"""CRITICAL: Worktree Context
You are working in a git worktree, NOT the main repository.
- Branch: {ctx.branch_name}
- Worktree path: {ctx.cwd}
- Main repo: {ctx.extra.get("main_repo_path", "unknown")}

Changes in this worktree are isolated from the main repository.
Commit your changes to the worktree branch when done.

---

"""
        return warning + original_prompt

    def _generate_worktree_path(self, branch_name: str, project_name: str) -> str:
        """Generate a unique worktree path in ~/.gobby/worktrees/."""
        # Sanitize branch name for use in path
        safe_branch = branch_name.replace("/", "-").replace("\\", "-")
        return str(Path.home() / ".gobby" / "worktrees" / project_name / safe_branch)
