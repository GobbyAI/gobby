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
import json
import logging
import os
import subprocess  # nosec B404 # needed for git error handling
import time
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from gobby.storage.tasks import TaskArtifactManager

logger = logging.getLogger(__name__)


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
            if Path(existing.worktree_path).is_dir():
                # Use existing worktree
                return IsolationContext(
                    cwd=existing.worktree_path,
                    branch_name=existing.branch_name,
                    worktree_id=existing.id,
                    isolation_type="worktree",
                    extra={"main_repo_path": str(self._git_manager.repo_path)},
                )
            else:
                # Stale record — directory gone, clean up and fall through to create new

                logger.warning(
                    f"Worktree directory missing: {existing.worktree_path} (cleaning up stale record {existing.id})",
                )
                self._worktree_storage.delete(existing.id)

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

            logger.info(
                f"Using local branch '{base_branch}' for worktree "
                f"({unpushed_count} unpushed commits)"
            )

        # Generate worktree path
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

        if config.task_id is not None:
            base_commit_sha = await asyncio.to_thread(_capture_base_commit_sha, worktree_path)
            await asyncio.to_thread(
                TaskArtifactManager(self._worktree_storage.db).set_artifacts_atomic,
                config.task_id,
                worktree_path=worktree_path,
                worktree_id=worktree.id,
                base_commit_sha=base_commit_sha,
            )

        # Copy CLI hooks to worktree so hooks fire correctly
        await _copy_cli_hooks(
            source_path=str(self._git_manager.repo_path),
            target_path=worktree_path,
            provider=config.provider,
        )

        # Ensure project.json has parent_project_path for workflow discovery
        from gobby.utils.project_context import ensure_project_json_for_isolation

        await asyncio.to_thread(
            ensure_project_json_for_isolation,
            str(self._git_manager.repo_path),
            worktree_path,
        )

        # Patch MCP config so agents use the main repo's gobby code
        await _patch_mcp_config_for_isolation(
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
            extra={"main_repo_path": str(self._git_manager.repo_path)},
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
            if Path(existing.clone_path).is_dir():
                # Use existing clone
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
                    f"Clone directory missing: {existing.clone_path} (cleaning up stale record {existing.id})",
                )
                self._clone_storage.delete(existing.id)

        # Determine base branch - use parent's current branch if default "main" was passed
        base_branch = config.base_branch
        use_local = False

        # If base_branch is the default "main", check if parent is on a different branch
        if self._git_manager is not None:
            current_branch = self._git_manager.get_current_branch()
            if current_branch and base_branch == "main" and current_branch != "main":
                # Use parent's current branch instead
                base_branch = current_branch

                logger.info(f"Using parent's current branch '{base_branch}' for clone")

            # Check for unpushed commits on the base branch
            try:
                has_unpushed, unpushed_count = self._git_manager.has_unpushed_commits(base_branch)
                if has_unpushed:
                    use_local = True
                    logger.info(
                        f"Using local repo for clone "
                        f"({unpushed_count} unpushed commits on '{base_branch}')"
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

        if config.task_id is not None:
            base_commit_sha = await asyncio.to_thread(_capture_base_commit_sha, clone_path)
            await asyncio.to_thread(
                TaskArtifactManager(self._clone_storage.db).set_artifacts_atomic,
                config.task_id,
                clone_path=clone_path,
                clone_id=clone.id,
                base_commit_sha=base_commit_sha,
            )

        # Copy CLI hooks to clone so hooks fire correctly
        await _copy_cli_hooks(
            source_path=config.project_path,
            target_path=clone_path,
            provider=config.provider,
        )

        # Ensure project.json has parent_project_path for workflow discovery
        from gobby.utils.project_context import ensure_project_json_for_isolation

        await asyncio.to_thread(
            ensure_project_json_for_isolation,
            config.project_path,
            clone_path,
        )

        # Patch MCP config so agents use the main repo's gobby code
        await _patch_mcp_config_for_isolation(
            main_repo_path=config.project_path,
            isolated_path=clone_path,
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
        """Generate a unique clone path in ~/.gobby/clones/."""
        # Sanitize branch name for use in path
        safe_branch = branch_name.replace("/", "-").replace("\\", "-")
        return str(Path.home() / ".gobby" / "clones" / project_name / safe_branch)


async def _copy_cli_hooks(
    source_path: str,
    target_path: str,
    provider: str,
) -> None:
    """Copy CLI-specific hooks to an isolated environment.

    Without these hooks, the spawned agent won't trigger SessionStart
    and other lifecycle hooks, breaking Gobby integration.

    Args:
        source_path: Path to the source repository
        target_path: Path to the isolated environment (worktree or clone)
        provider: CLI provider (gemini, claude, codex, droid)
    """
    import shutil

    if provider == "droid":
        await _copy_droid_hooks_for_isolation(target_path)
        return

    cli_dirs = {
        "gemini": ".gemini",
        "claude": ".claude",
        "codex": ".codex",
    }

    cli_dir = cli_dirs.get(provider)
    if not cli_dir:
        logger.debug(f"No CLI hooks directory defined for provider: {provider}")
        return

    src_path = Path(source_path) / cli_dir
    dst_path = Path(target_path) / cli_dir

    if not src_path.exists():
        logger.debug(f"CLI hooks directory not found in source repo: {src_path}")
        return

    try:
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


async def _copy_droid_hooks_for_isolation(target_path: str) -> None:
    """Ensure isolated Droid sessions have Gobby hooks.

    Droid normally reads user-global ``~/.factory/hooks/hooks.json`` regardless
    of ``--cwd``, but project-local Factory config can shadow that file and
    global inheritance is hard to prove in automated isolation tests. Writing
    Gobby-owned hook entries into the isolated worktree or clone gives spawned
    Droid agents deterministic lifecycle hooks while preserving user entries.
    """
    hooks_path = Path(target_path) / ".factory" / "hooks" / "hooks.json"
    try:
        await asyncio.to_thread(_write_droid_isolation_hooks, hooks_path)
        logger.info(f"Wrote Droid isolation hooks to {hooks_path}")
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        logger.warning(
            f"Failed to write Droid isolation hooks: target={hooks_path}",
            exc_info=True,
        )


def _write_droid_isolation_hooks(hooks_path: Path) -> None:
    existing_settings = _load_json_object(hooks_path)
    gobby_settings = _load_droid_isolation_hooks_template()
    updated_settings = _merge_droid_isolation_hooks(existing_settings, gobby_settings)
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(updated_settings, indent=2) + "\n")


def _load_droid_isolation_hooks_template() -> dict[str, Any]:
    from gobby.cli.installers.hook_commands import rewrite_hook_template_commands
    from gobby.paths import get_install_dir

    template_path = get_install_dir() / "droid" / "hooks-template.json"
    if not template_path.exists():
        raise FileNotFoundError(f"Missing Droid hooks template: {template_path}")

    template = _load_json_object(template_path)
    hooks_dir = Path(os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks")))
    rewrite_hook_template_commands(
        template,
        cli_name="droid",
        hooks_dir=hooks_dir.expanduser(),
    )
    return template


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open() as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _merge_droid_isolation_hooks(
    existing_settings: dict[str, Any],
    gobby_settings: dict[str, Any],
) -> dict[str, Any]:
    from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
    from gobby.cli.installers.hook_commands import config_contains_gobby_hook

    updated = deepcopy(existing_settings)
    hooks = updated.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        updated["hooks"] = hooks

    gobby_hooks = gobby_settings.get("hooks")
    if not isinstance(gobby_hooks, dict):
        raise ValueError("Droid hooks template does not contain a hooks object")

    for hook_type in DROID_PASCAL_HOOK_NAMES:
        hook_config = gobby_hooks.get(hook_type)
        if not isinstance(hook_config, list) or not hook_config:
            raise ValueError(f"Droid hooks template missing hook type: {hook_type}")

        current_config = hooks.get(hook_type)
        preserved: list[Any] = []
        if isinstance(current_config, list):
            preserved = [
                deepcopy(entry) for entry in current_config if not config_contains_gobby_hook(entry)
            ]
        hooks[hook_type] = preserved + deepcopy(hook_config)

    return updated


async def _patch_mcp_config_for_isolation(
    main_repo_path: str,
    isolated_path: str,
    provider: str,
) -> None:
    """Patch MCP server config so isolated agents use the main repo's gobby code.

    Without this, ``uv run gobby mcp-server`` resolves from the isolated
    environment's ``pyproject.toml``/``uv.lock``, which may be on an old
    branch and missing recent fixes.

    Writes a ``.mcp.json`` in the isolated directory that forces
    ``--project <main_repo_path>`` so ``uv`` resolves from the main repo.

    For Claude provider, also patches ``~/.claude.json`` to register the
    isolated path as a project with the correct MCP server config.
    """
    import json

    mcp_config = {
        "mcpServers": {
            "gobby": {
                "command": "uv",
                "args": [
                    "run",
                    "--project",
                    main_repo_path,
                    "gobby",
                    "mcp-server",
                ],
            }
        }
    }

    mcp_json_path = Path(isolated_path) / ".mcp.json"
    try:
        await asyncio.to_thread(mcp_json_path.write_text, json.dumps(mcp_config, indent=2) + "\n")
        logger.info(f"Wrote MCP config to {mcp_json_path}")
    except OSError as e:
        logger.warning(f"Failed to write .mcp.json to {isolated_path}: {e}")
        return

    # For Claude provider, register the isolated path in ~/.claude.json
    if provider == "claude":
        claude_json_path = Path.home() / ".claude.json"
        try:

            def _patch_claude_json() -> None:
                import os
                import tempfile

                data: dict[str, Any] = {}
                if claude_json_path.exists():
                    try:
                        data = json.loads(claude_json_path.read_text())
                    except json.JSONDecodeError:
                        logger.warning("Malformed ~/.claude.json, re-initializing")
                        data = {}

                projects = data.setdefault("projects", {})
                projects[isolated_path] = {
                    "mcpServers": mcp_config["mcpServers"],
                }

                # Atomic write via tempfile + os.replace to avoid TOCTOU race
                fd, tmp_path = tempfile.mkstemp(dir=str(claude_json_path.parent), suffix=".tmp")
                try:
                    os.write(fd, (json.dumps(data, indent=2) + "\n").encode())
                    os.close(fd)
                    os.replace(tmp_path, str(claude_json_path))
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise

            await asyncio.to_thread(_patch_claude_json)
            logger.info(f"Registered isolated path in ~/.claude.json: {isolated_path}")
        except Exception as e:
            logger.warning(f"Failed to patch ~/.claude.json for {isolated_path}: {e}")


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
