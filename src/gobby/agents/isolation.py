"""
Isolation Handlers for Unified spawn_agent API.

This module provides the abstraction layer for different isolation modes:
- none: Work in the current directory (no isolation)
- worktree: Create/reuse a git worktree for isolated work
- clone: Create a shallow clone for full isolation

Each handler implements the IsolationHandler ABC to provide:
- Environment preparation (worktree/clone creation)
- Context prompt building (adding isolation warnings)
- Branch name generation
"""

import asyncio
import subprocess  # nosec B404 - needed for git error handling
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class IsolationContext:
    """Result of environment preparation."""

    cwd: str
    branch_name: str | None = None
    worktree_id: str | None = None
    clone_id: str | None = None
    isolation_type: Literal["none", "worktree", "clone"] = "none"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpawnConfig:
    """Configuration passed to isolation handlers."""

    prompt: str
    task_id: str | None
    task_title: str | None
    task_seq_num: int | None
    branch_name: str | None
    branch_prefix: str | None
    base_branch: str
    project_id: str
    project_path: str
    provider: str
    parent_session_id: str


def generate_branch_name(config: SpawnConfig) -> str:
    """
    Auto-generate branch name from task or fallback to prefix+timestamp.

    Priority:
    1. Explicit branch_name if provided
    2. task-{seq_num}-{slugified_title} if task info available
    3. {branch_prefix}{timestamp} as fallback (default prefix: "agent/")
    """
    if config.branch_name:
        return config.branch_name

    if config.task_seq_num and config.task_title:
        # Generate slug from task title
        slug = config.task_title.lower().replace(" ", "-")
        # Keep only alphanumeric and hyphens
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
        # Truncate to 40 chars
        slug = slug[:40]
        return f"task-{config.task_seq_num}-{slug}"

    # Fallback to prefix + timestamp
    prefix = config.branch_prefix or "agent/"
    return f"{prefix}{int(time.time())}"


class IsolationHandler(ABC):
    """Abstract base class for isolation handlers."""

    @abstractmethod
    async def prepare_environment(self, config: SpawnConfig) -> IsolationContext:
        """
        Prepare isolated environment (worktree/clone creation).

        Args:
            config: Spawn configuration with project and task info

        Returns:
            IsolationContext with cwd and isolation metadata
        """

    @abstractmethod
    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """
        Clean up partially created environment after prepare_environment failure.

        Handlers track what was created during prepare_environment.
        This method reverses those partial side effects.

        Args:
            config: The same SpawnConfig passed to prepare_environment
        """

    @abstractmethod
    def build_context_prompt(self, original_prompt: str, ctx: IsolationContext) -> str:
        """
        Build prompt with isolation context warnings.

        Args:
            original_prompt: The original user prompt
            ctx: Isolation context from prepare_environment

        Returns:
            Enhanced prompt with isolation context (or unchanged for current)
        """


class NoneIsolationHandler(IsolationHandler):
    """
    No isolation - work in current directory.

    This is the simplest handler that just returns the project path
    as the working directory without any git branch changes.
    """

    async def prepare_environment(self, config: SpawnConfig) -> IsolationContext:
        """Return project path as working directory."""
        return IsolationContext(
            cwd=config.project_path,
            isolation_type="none",
        )

    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """No-op - nothing to clean up for current directory."""

    def build_context_prompt(self, original_prompt: str, ctx: IsolationContext) -> str:
        """Return prompt unchanged - no additional context needed."""
        return original_prompt


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

        - Generate branch name if not provided
        - Check for existing worktree for the branch
        - Determine base branch (use parent's current branch if not specified)
        - Check for unpushed commits and use local ref if needed
        - Create new worktree if needed
        - Return IsolationContext with worktree info
        """
        # Reset partial state
        self._created_worktree_path = None
        self._created_worktree_id = None

        branch_name = generate_branch_name(config)

        # Check if worktree already exists for this branch
        existing = self._worktree_storage.get_by_branch(config.project_id, branch_name)
        if existing:
            # Use existing worktree
            return IsolationContext(
                cwd=existing.worktree_path,
                branch_name=existing.branch_name,
                worktree_id=existing.id,
                isolation_type="worktree",
                extra={"main_repo_path": self._git_manager.repo_path},
            )

        # Determine base branch - use parent's current branch if default "main" was passed
        base_branch = config.base_branch
        use_local = False

        # If base_branch is the default "main", check if parent is on a different branch
        current_branch = self._git_manager.get_current_branch()
        if current_branch and base_branch == "main" and current_branch != "main":
            # Use parent's current branch instead
            base_branch = current_branch

        # Check for unpushed commits on the base branch
        has_unpushed, unpushed_count = self._git_manager.has_unpushed_commits(base_branch)
        if has_unpushed:
            # Use local branch ref to preserve unpushed commits
            use_local = True
            import logging

            logger = logging.getLogger(__name__)
            logger.info(
                f"Using local branch '{base_branch}' for worktree "
                f"({unpushed_count} unpushed commits)"
            )

        # Generate worktree path
        from pathlib import Path

        project_name = Path(self._git_manager.repo_path).name
        worktree_path = self._generate_worktree_path(branch_name, project_name)

        # Create git worktree
        result = self._git_manager.create_worktree(
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
        worktree = self._worktree_storage.create(
            project_id=config.project_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            base_branch=config.base_branch,
            task_id=config.task_id,
        )

        # Track storage record for cleanup
        self._created_worktree_id = worktree.id

        # Copy CLI hooks to worktree so hooks fire correctly
        await self._copy_cli_hooks(
            main_repo_path=self._git_manager.repo_path,
            worktree_path=worktree_path,
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
            extra={"main_repo_path": self._git_manager.repo_path},
        )

    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """Clean up partially created worktree on prepare failure."""
        import logging

        logger = logging.getLogger(__name__)

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
        """Generate a unique worktree path in temp directory."""
        import tempfile

        # Sanitize branch name for use in path
        safe_branch = branch_name.replace("/", "-").replace("\\", "-")
        worktree_dir = tempfile.gettempdir()
        return f"{worktree_dir}/gobby-worktrees/{project_name}/{safe_branch}"

    async def _copy_cli_hooks(
        self,
        main_repo_path: str,
        worktree_path: str,
        provider: str,
    ) -> None:
        """
        Copy CLI-specific hooks to the worktree.

        Without these hooks, the spawned agent won't trigger SessionStart
        and other lifecycle hooks, breaking Gobby integration.

        Args:
            main_repo_path: Path to the main repository
            worktree_path: Path to the newly created worktree
            provider: CLI provider (gemini, claude, codex)
        """
        import asyncio
        import logging
        import shutil
        from pathlib import Path

        logger = logging.getLogger(__name__)

        # Map provider to CLI hook directory
        cli_dirs = {
            "gemini": ".gemini",
            "claude": ".claude",
            "codex": ".codex",
            "cursor": ".claude",
            "windsurf": ".claude",
            "copilot": ".claude",
        }

        cli_dir = cli_dirs.get(provider)
        if not cli_dir:
            logger.debug(f"No CLI hooks directory defined for provider: {provider}")
            return

        src_path = Path(main_repo_path) / cli_dir
        dst_path = Path(worktree_path) / cli_dir

        if not src_path.exists():
            logger.debug(f"CLI hooks directory not found in main repo: {src_path}")
            return

        try:
            # Copy entire CLI hooks directory (non-blocking)
            await asyncio.to_thread(shutil.copytree, src_path, dst_path, dirs_exist_ok=True)
            logger.info(f"Copied CLI hooks from {src_path} to {dst_path}")
        except shutil.Error:
            logger.warning(
                f"Failed to copy CLI hooks: provider={provider}, src={src_path}, dst={dst_path}",
                exc_info=True,
            )
        except OSError:
            logger.warning(
                f"Filesystem error copying CLI hooks: provider={provider}, src={src_path}, dst={dst_path}",
                exc_info=True,
            )


class CloneIsolationHandler(IsolationHandler):
    """
    Clone isolation - create a shallow clone for full isolation.

    This handler:
    - Checks for existing clones by branch name
    - Creates new shallow clones if needed
    - Adds CRITICAL context warning to prompt
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
        self._created_clone_path: str | None = None
        self._created_clone_id: str | None = None

    async def prepare_environment(self, config: SpawnConfig) -> IsolationContext:
        """
        Prepare clone environment.

        - Generate branch name if not provided
        - Check for existing clone for the branch
        - Create new shallow clone if needed
        - Return IsolationContext with clone info
        """
        # Reset partial state
        self._created_clone_path = None
        self._created_clone_id = None

        branch_name = generate_branch_name(config)

        # Check if clone already exists for this branch
        existing = self._clone_storage.get_by_branch(config.project_id, branch_name)
        if existing:
            # Use existing clone
            return IsolationContext(
                cwd=existing.clone_path,
                branch_name=existing.branch_name,
                clone_id=existing.id,
                isolation_type="clone",
                extra={"source_repo": config.project_path},
            )

        # Determine base branch - use parent's current branch if default "main" was passed
        base_branch = config.base_branch
        use_local = False

        # If base_branch is the default "main", check if parent is on a different branch
        if self._git_manager is not None:
            current_branch = self._git_manager.get_current_branch()
            if current_branch and base_branch == "main" and current_branch != "main":
                # Use parent's current branch instead
                base_branch = current_branch
                import logging

                logger = logging.getLogger(__name__)
                logger.info(f"Using parent's current branch '{base_branch}' for clone")

            # Check for unpushed commits on the base branch
            try:
                has_unpushed, unpushed_count = self._git_manager.has_unpushed_commits(base_branch)
                if has_unpushed:
                    use_local = True
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.info(
                        f"Using local repo for clone "
                        f"({unpushed_count} unpushed commits on '{base_branch}')"
                    )
            except (subprocess.CalledProcessError, OSError):
                import logging

                logger = logging.getLogger(__name__)
                logger.warning(
                    "Failed to check unpushed commits for clone, using remote",
                    exc_info=True,
                )

        # Generate clone path
        from pathlib import Path

        project_name = Path(config.project_path).name
        clone_path = self._generate_clone_path(branch_name, project_name)

        # Create clone (full when use_local, shallow otherwise)
        result = self._clone_manager.create_clone(
            clone_path=clone_path,
            branch_name=branch_name,
            base_branch=base_branch,
            shallow=not use_local,
            use_local=use_local,
        )

        if not result.success:
            raise RuntimeError(f"Failed to create clone: {result.error}")

        # Track for cleanup — clone exists on disk now
        self._created_clone_path = clone_path

        # Record in storage
        clone = self._clone_storage.create(
            project_id=config.project_id,
            branch_name=branch_name,
            clone_path=clone_path,
            base_branch=base_branch,
            task_id=config.task_id,
        )

        # Track storage record for cleanup
        self._created_clone_id = clone.id

        # Copy CLI hooks to clone so hooks fire correctly
        await self._copy_cli_hooks(
            source_repo_path=config.project_path,
            clone_path=clone_path,
            provider=config.provider,
        )

        # Success — clear partial state
        self._created_clone_path = None
        self._created_clone_id = None

        return IsolationContext(
            cwd=clone.clone_path,
            branch_name=clone.branch_name,
            clone_id=clone.id,
            isolation_type="clone",
            extra={"source_repo": config.project_path},
        )

    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """Clean up partially created clone on prepare failure."""
        import logging

        logger = logging.getLogger(__name__)

        if self._created_clone_path:
            try:
                await asyncio.to_thread(
                    self._clone_manager.delete_clone,
                    clone_path=self._created_clone_path,
                    force=True,
                )
                logger.info(f"Cleaned up partial clone: {self._created_clone_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up clone {self._created_clone_path}: {e}")

        if self._created_clone_id:
            try:
                self._clone_storage.delete(self._created_clone_id)
                logger.info(f"Cleaned up clone storage record: {self._created_clone_id}")
            except Exception as e:
                logger.warning(f"Failed to clean up clone record {self._created_clone_id}: {e}")

        self._created_clone_path = None
        self._created_clone_id = None

    def build_context_prompt(self, original_prompt: str, ctx: IsolationContext) -> str:
        """
        Build prompt with CRITICAL clone context warning.

        Prepends isolation context to help the agent understand it's
        working in a clone, not the original repository.
        """
        warning = f"""CRITICAL: Clone Context
You are working in a shallow clone, NOT the original repository.
- Branch: {ctx.branch_name}
- Clone path: {ctx.cwd}
- Source repo: {ctx.extra.get("source_repo", "unknown")}

Changes in this clone are fully isolated from the original repository.
Push your changes when ready to share with the original.

---

"""
        return warning + original_prompt

    def _generate_clone_path(self, branch_name: str, project_name: str) -> str:
        """Generate a unique clone path in temp directory."""
        import tempfile

        # Sanitize branch name for use in path
        safe_branch = branch_name.replace("/", "-").replace("\\", "-")
        clone_dir = tempfile.gettempdir()
        return f"{clone_dir}/gobby-clones/{project_name}/{safe_branch}"

    async def _copy_cli_hooks(
        self,
        source_repo_path: str,
        clone_path: str,
        provider: str,
    ) -> None:
        """
        Copy CLI-specific hooks to the clone.

        Without these hooks, the spawned agent won't trigger SessionStart
        and other lifecycle hooks, breaking Gobby integration.

        Args:
            source_repo_path: Path to the source repository
            clone_path: Path to the newly created clone
            provider: CLI provider (gemini, claude, codex)
        """
        import asyncio
        import logging
        import shutil
        from pathlib import Path

        logger = logging.getLogger(__name__)

        # Map provider to CLI hook directory
        cli_dirs = {
            "gemini": ".gemini",
            "claude": ".claude",
            "codex": ".codex",
            "cursor": ".claude",
            "windsurf": ".claude",
            "copilot": ".claude",
        }

        cli_dir = cli_dirs.get(provider)
        if not cli_dir:
            logger.debug(f"No CLI hooks directory defined for provider: {provider}")
            return

        src_path = Path(source_repo_path) / cli_dir
        dst_path = Path(clone_path) / cli_dir

        if not src_path.exists():
            logger.debug(f"CLI hooks directory not found in source repo: {src_path}")
            return

        try:
            # Copy entire CLI hooks directory (non-blocking)
            await asyncio.to_thread(shutil.copytree, src_path, dst_path, dirs_exist_ok=True)
            logger.info(f"Copied CLI hooks from {src_path} to {dst_path}")
        except shutil.Error:
            logger.warning(
                f"Failed to copy CLI hooks: provider={provider}, src={src_path}, dst={dst_path}",
                exc_info=True,
            )
        except OSError:
            logger.warning(
                f"Filesystem error copying CLI hooks: provider={provider}, src={src_path}, dst={dst_path}",
                exc_info=True,
            )


def get_isolation_handler(
    mode: Literal["none", "worktree", "clone"],
    *,
    git_manager: Any | None = None,
    worktree_storage: Any | None = None,
    clone_manager: Any | None = None,
    clone_storage: Any | None = None,
) -> IsolationHandler:
    """
    Factory function to get the appropriate isolation handler.

    Args:
        mode: Isolation mode - 'none', 'worktree', or 'clone'
        git_manager: Git manager for worktree operations (required for 'worktree')
        worktree_storage: Storage for worktree records (required for 'worktree')
        clone_manager: Git manager for clone operations (required for 'clone')
        clone_storage: Storage for clone records (required for 'clone')

    Returns:
        IsolationHandler instance for the specified mode

    Raises:
        ValueError: If mode is unknown or required dependencies are missing
    """
    if mode == "none":
        return NoneIsolationHandler()

    if mode == "worktree":
        if git_manager is None or worktree_storage is None:
            raise ValueError("git_manager and worktree_storage are required for worktree isolation")
        return WorktreeIsolationHandler(
            git_manager=git_manager,
            worktree_storage=worktree_storage,
        )

    if mode == "clone":
        if clone_manager is None or clone_storage is None:
            raise ValueError("clone_manager and clone_storage are required for clone isolation")
        return CloneIsolationHandler(
            clone_manager=clone_manager,
            clone_storage=clone_storage,
            git_manager=git_manager,  # For branch detection
        )

    raise ValueError(f"Unknown isolation mode: {mode}")
