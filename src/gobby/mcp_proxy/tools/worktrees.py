"""
Internal MCP tools for Gobby Worktree Management.

Exposes functionality for:
- Creating git worktrees for isolated development
- Managing worktree lifecycle (claim, release, cleanup)
- Syncing worktrees with main branch
- Spawning agents in worktrees

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from __future__ import annotations

import json
import logging
import platform
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.project_context import get_project_context
from gobby.worktrees.git import WorktreeGitManager

if TYPE_CHECKING:
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)

# Cache for WorktreeGitManager instances per repo path
_git_manager_cache: dict[str, WorktreeGitManager] = {}


def _get_worktree_base_dir() -> Path:
    """
    Get the base directory for worktrees.

    Uses the system temp directory:
    - macOS/Linux: /tmp/gobby-worktrees/
    - Windows: %TEMP%/gobby-worktrees/

    Returns:
        Path to worktree base directory (creates if needed)
    """
    if platform.system() == "Windows":
        # Windows: use %TEMP% (typically C:\\Users\\<user>\\AppData\\Local\\Temp)
        base = Path(tempfile.gettempdir()) / "gobby-worktrees"
    else:
        # macOS/Linux: use /tmp for better isolation (tmpfs, cleared on reboot)
        # Resolve symlink on macOS (/tmp -> /private/tmp) for consistent paths
        base = Path("/tmp").resolve() / "gobby-worktrees"  # nosec B108

    base.mkdir(parents=True, exist_ok=True)
    return base


def _generate_worktree_path(branch_name: str, project_name: str | None = None) -> str:
    """
    Generate a worktree path in the temp directory.

    Args:
        branch_name: Branch name (used as directory name)
        project_name: Optional project name for namespacing

    Returns:
        Full path for the worktree
    """
    base = _get_worktree_base_dir()

    # Sanitize branch name for filesystem (replace / with -)
    safe_branch = branch_name.replace("/", "-")

    if project_name:
        # Namespace by project: /tmp/gobby-worktrees/project-name/branch-name
        return str(base / project_name / safe_branch)
    else:
        # No project namespace: /tmp/gobby-worktrees/branch-name
        return str(base / safe_branch)


def _resolve_project_context(
    project_path: str | None,
    default_git_manager: WorktreeGitManager | None,
    default_project_id: str | None,
) -> tuple[WorktreeGitManager | None, str | None, str | None]:
    """
    Resolve project context from project_path or fall back to defaults.

    Args:
        project_path: Path to project directory (cwd from caller).
        default_git_manager: Registry-level git manager (may be None).
        default_project_id: Registry-level project ID (may be None).

    Returns:
        Tuple of (git_manager, project_id, error_message).
        If error_message is not None, the other values should not be used.
    """
    if project_path:
        # Resolve from provided path
        path = Path(project_path)
        if not path.exists():
            return None, None, f"Project path does not exist: {project_path}"

        project_ctx = get_project_context(path)
        if not project_ctx:
            return None, None, f"No .gobby/project.json found in {project_path}"

        resolved_project_id = project_ctx.get("id")
        resolved_path = project_ctx.get("project_path", str(path))

        # Get or create git manager for this path
        if resolved_path not in _git_manager_cache:
            try:
                _git_manager_cache[resolved_path] = WorktreeGitManager(resolved_path)
            except ValueError as e:
                return None, None, f"Invalid git repository: {e}"

        return _git_manager_cache[resolved_path], resolved_project_id, None

    # Fall back to defaults
    if default_git_manager is not None and default_project_id is not None:
        return default_git_manager, default_project_id, None

    # Try resolving from context var (set per-MCP-call from session)
    ctx = get_project_context()
    if ctx and ctx.get("id"):
        resolved_path = ctx.get("project_path")
        if resolved_path:
            if resolved_path not in _git_manager_cache:
                try:
                    _git_manager_cache[resolved_path] = WorktreeGitManager(resolved_path)
                except ValueError as e:
                    return None, None, f"Failed to initialize git manager for {resolved_path}: {e}"
            return _git_manager_cache[resolved_path], ctx["id"], None

    return (
        None,
        None,
        "No project_path provided and no project context available. Pass project_path parameter.",
    )


def _copy_project_json_to_worktree(
    repo_path: str | Path,
    worktree_path: str | Path,
) -> None:
    """
    Copy .gobby/project.json from main repo to worktree, adding parent reference.

    This ensures worktree sessions:
    - Use the same project_id as the parent repo
    - Can discover the parent project path for workflow lookup

    Args:
        repo_path: Path to main repository
        worktree_path: Path to worktree directory
    """
    main_gobby_dir = Path(repo_path) / ".gobby"
    main_project_json = main_gobby_dir / "project.json"
    worktree_gobby_dir = Path(worktree_path) / ".gobby"

    if main_project_json.exists():
        try:
            worktree_gobby_dir.mkdir(parents=True, exist_ok=True)
            worktree_project_json = worktree_gobby_dir / "project.json"
            if not worktree_project_json.exists():
                # Read, add parent reference, write
                with open(main_project_json) as f:
                    data = json.load(f)

                data["parent_project_path"] = str(Path(repo_path).resolve())

                with open(worktree_project_json, "w") as f:
                    json.dump(data, f, indent=2)

                logger.info("Created project.json in worktree with parent reference")
        except Exception as e:
            logger.warning(f"Failed to create project.json in worktree: {e}")


def _install_provider_hooks(
    provider: Literal["claude", "gemini", "codex", "antigravity", "cursor", "windsurf", "copilot"]
    | None,
    worktree_path: str | Path,
) -> bool:
    """
    Install CLI hooks for the specified provider in the worktree.

    Args:
        provider: Provider name ('claude', 'gemini', 'antigravity', 'cursor', 'windsurf', 'copilot', or None)
        worktree_path: Path to worktree directory

    Returns:
        True if hooks were successfully installed, False otherwise
    """
    if not provider:
        return False

    worktree_path_obj = Path(worktree_path)
    try:
        if provider == "claude":
            from gobby.cli.installers.claude import install_claude

            result = install_claude(worktree_path_obj)
            if result["success"]:
                logger.info(f"Installed Claude hooks in worktree: {worktree_path}")
                return True
            else:
                logger.warning(f"Failed to install Claude hooks: {result.get('error')}")
        elif provider in ("cursor", "windsurf", "copilot"):
            # These editors use Claude hooks format
            from gobby.cli.installers.claude import install_claude

            result = install_claude(worktree_path_obj)
            if result["success"]:
                logger.info(f"Installed {provider} hooks in worktree: {worktree_path}")
                return True
            else:
                logger.warning(f"Failed to install {provider} hooks: {result.get('error')}")
        elif provider == "gemini":
            from gobby.cli.installers.gemini import install_gemini

            result = install_gemini(worktree_path_obj)
            if result["success"]:
                logger.info(f"Installed Gemini hooks in worktree: {worktree_path}")
                return True
            else:
                logger.warning(f"Failed to install Gemini hooks: {result.get('error')}")
        elif provider == "antigravity":
            from gobby.cli.installers.antigravity import install_antigravity

            result = install_antigravity(worktree_path_obj)
            if result["success"]:
                logger.info(f"Installed Antigravity hooks in worktree: {worktree_path}")
                return True
            else:
                logger.warning(f"Failed to install Antigravity hooks: {result.get('error')}")
        # Note: codex uses CODEX_NOTIFY_SCRIPT env var, not project-level hooks
    except Exception as e:
        logger.warning(f"Failed to install {provider} hooks in worktree: {e}")
    return False


def create_worktrees_registry(
    worktree_storage: LocalWorktreeManager,
    git_manager: WorktreeGitManager | None = None,
    project_id: str | None = None,
    session_manager: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a worktree tool registry with all worktree-related tools.

    Args:
        worktree_storage: LocalWorktreeManager for database operations.
        git_manager: WorktreeGitManager for git operations.
        project_id: Default project ID for operations.
        session_manager: Session manager for resolving session references.

    Returns:
        InternalToolRegistry with all worktree tools registered.
    """

    def _resolve_session_id(ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        if session_manager is None:
            return ref  # No resolution available, return as-is
        ctx = get_project_context()
        proj_id = ctx.get("id") if ctx else project_id
        return str(session_manager.resolve_session_reference(ref, proj_id))

    registry = InternalToolRegistry(
        name="gobby-worktrees",
        description="Git worktree management - create, manage, and cleanup isolated development directories",
    )

    @registry.tool(
        name="create_worktree",
        description="Create a new git worktree for isolated development.",
    )
    async def create_worktree(
        branch_name: str,
        base_branch: str = "main",
        task_id: str | None = None,
        worktree_path: str | None = None,
        create_branch: bool = True,
        project_path: str | None = None,
        provider: Literal[
            "claude", "gemini", "codex", "antigravity", "cursor", "windsurf", "copilot"
        ]
        | None = None,
    ) -> dict[str, Any]:
        """
        Create a new git worktree.

        Args:
            branch_name: Name for the new branch.
            base_branch: Branch to base the worktree on (default: main).
            task_id: Optional task ID to link to this worktree.
            worktree_path: Optional custom path (defaults to ../{branch_name}).
            create_branch: Whether to create a new branch (default: True).
            project_path: Path to project directory (pass cwd from CLI).
            provider: CLI provider to install hooks for (claude, gemini, codex, antigravity, cursor, windsurf, copilot).
                     If specified, installs hooks so agents can communicate with daemon.

        Returns:
            Dict with worktree ID, path, and branch info.
        """
        # Resolve project context
        resolved_git_mgr, resolved_project_id, error = _resolve_project_context(
            project_path, git_manager, project_id
        )
        if error:
            return {"success": False, "error": error}

        # Type narrowing: if no error, these are guaranteed non-None
        if resolved_git_mgr is None or resolved_project_id is None:
            raise RuntimeError("Git manager or project ID unexpectedly None")

        # Check if branch already exists as a worktree
        existing = worktree_storage.get_by_branch(resolved_project_id, branch_name)
        if existing:
            return {
                "success": False,
                "error": f"Worktree already exists for branch '{branch_name}'",
                "existing_worktree_id": existing.id,
                "existing_path": existing.worktree_path,
            }

        # Generate default worktree path if not provided
        if worktree_path is None:
            # Use temp directory (e.g., /tmp/gobby-worktrees/project-name/branch-name)
            project_name = Path(resolved_git_mgr.repo_path).name
            worktree_path = _generate_worktree_path(branch_name, project_name)

        # Create git worktree
        result = resolved_git_mgr.create_worktree(
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_branch=base_branch,
            create_branch=create_branch,
        )

        if not result.success:
            return {"success": False, "error": result.error or "Failed to create git worktree"}

        # Record in database
        worktree = worktree_storage.create(
            project_id=resolved_project_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            base_branch=base_branch,
            task_id=task_id,
        )

        # Copy project.json and install provider hooks
        _copy_project_json_to_worktree(resolved_git_mgr.repo_path, worktree.worktree_path)
        hooks_installed = _install_provider_hooks(provider, worktree.worktree_path)

        return {
            "success": True,
            "worktree_id": worktree.id,
            "worktree_path": worktree.worktree_path,
            "hooks_installed": hooks_installed,
        }

    @registry.tool(
        name="get_worktree",
        description="Get details of a specific worktree.",
    )
    async def get_worktree(worktree_id: str) -> dict[str, Any]:
        """
        Get worktree details by ID.

        Args:
            worktree_id: The worktree ID.

        Returns:
            Dict with full worktree details.
        """
        worktree = worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        # Get git status if manager available
        git_status = None
        if git_manager and Path(worktree.worktree_path).exists():
            status = git_manager.get_worktree_status(worktree.worktree_path)
            if status:
                git_status = {
                    "has_uncommitted_changes": status.has_uncommitted_changes,
                    "commits_ahead": status.ahead,
                    "commits_behind": status.behind,
                    "current_branch": status.branch,
                }

        return {
            "success": True,
            "worktree": worktree.to_dict(),
            "git_status": git_status,
        }

    @registry.tool(
        name="list_worktrees",
        description="List worktrees with optional filters. Accepts #N, N, UUID, or prefix for agent_session_id.",
    )
    async def list_worktrees(
        status: str | None = None,
        agent_session_id: str | None = None,
        limit: int | str = 50,
    ) -> dict[str, Any]:
        """
        List worktrees with optional filters.

        Args:
            status: Filter by status (active, stale, merged, abandoned).
            agent_session_id: Session reference (accepts #N, N, UUID, or prefix) to filter by owning session.
            limit: Maximum results (default: 50).

        Returns:
            Dict with list of worktrees.
        """
        # Handle string inputs from MCP
        limit = int(limit) if isinstance(limit, str) else limit

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        resolved_session_id = agent_session_id
        if agent_session_id:
            try:
                resolved_session_id = _resolve_session_id(agent_session_id)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        worktrees = worktree_storage.list_worktrees(
            project_id=project_id,
            status=status,
            agent_session_id=resolved_session_id,
            limit=limit,
        )

        return {
            "success": True,
            "worktrees": [
                {
                    "id": wt.id,
                    "branch_name": wt.branch_name,
                    "worktree_path": wt.worktree_path,
                    "status": wt.status,
                    "task_id": wt.task_id,
                    "agent_session_id": wt.agent_session_id,
                    "created_at": wt.created_at,
                }
                for wt in worktrees
            ],
            "count": len(worktrees),
        }

    @registry.tool(
        name="claim_worktree",
        description="Claim ownership of a worktree for an agent session. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def claim_worktree(
        worktree_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Claim a worktree for an agent session.

        Args:
            worktree_id: The worktree ID to claim.
            session_id: Session reference (accepts #N, N, UUID, or prefix) claiming ownership.

        Returns:
            Dict with success status.
        """
        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_session_id = _resolve_session_id(session_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        worktree = worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        if worktree.agent_session_id and worktree.agent_session_id != resolved_session_id:
            return {
                "success": False,
                "error": f"Worktree already claimed by session '{worktree.agent_session_id}'",
            }

        updated = worktree_storage.claim(worktree_id, resolved_session_id)
        if not updated:
            return {"success": False, "error": "Failed to claim worktree"}

        return {"success": True}

    @registry.tool(
        name="release_worktree",
        description="Release ownership of a worktree.",
    )
    async def release_worktree(worktree_id: str) -> dict[str, Any]:
        """
        Release a worktree from its current owner.

        Args:
            worktree_id: The worktree ID to release.

        Returns:
            Dict with success status.
        """
        worktree = worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = worktree_storage.release(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to release worktree"}

        return {"success": True}

    @registry.tool(
        name="delete_worktree",
        description="Delete a worktree (both git and database record).",
    )
    async def delete_worktree(
        worktree_id: str,
        force: bool | str = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Delete a worktree completely (handles all cleanup).

        This is the proper way to remove a worktree. It handles:
        - Removes the worktree directory and all temporary files
        - Cleans up git's worktree tracking (.git/worktrees/)
        - Deletes the associated git branch
        - Removes the Gobby database record

        Do NOT manually run `git worktree remove` - use this tool instead.

        Args:
            worktree_id: The worktree ID to delete (e.g., "wt-abc123").
            force: Force deletion even if there are uncommitted changes.
            project_path: Optional path to project root to resolve git context.

        Returns:
            Dict with success status.
        """
        # Handle string inputs from MCP
        force = force in (True, "true", "True", "1") if isinstance(force, str) else force

        worktree = worktree_storage.get(worktree_id)

        if not worktree:
            # Idempotent: already deleted = success
            return {"success": True, "already_deleted": True}

        # Resolve git manager
        resolved_git_mgr = git_manager  # Start with the module-level git_manager
        if project_path:
            try:
                # _resolve_project_context is defined in this module scope
                mgr, _, _ = _resolve_project_context(project_path, resolved_git_mgr, None)
                if mgr:
                    resolved_git_mgr = mgr
            except (ValueError, OSError) as e:
                logger.debug(
                    f"Failed to resolve project context for project_path={project_path}: {e}"
                )

        # Check if worktree path exists
        worktree_exists = Path(worktree.worktree_path).exists()

        # Check for uncommitted changes if not forcing (only if path exists)
        if resolved_git_mgr and worktree_exists:
            status = resolved_git_mgr.get_worktree_status(worktree.worktree_path)
            if status and status.has_uncommitted_changes and not force:
                return {
                    "success": False,
                    "error": "Worktree has uncommitted changes. Use force=True to delete anyway.",
                    "uncommitted_changes": True,
                }

        # Delete git worktree (only if path exists - handles orphaned DB records)
        if resolved_git_mgr and worktree_exists:
            result = resolved_git_mgr.delete_worktree(
                worktree.worktree_path,
                force=force,
                delete_branch=True,
                branch_name=worktree.branch_name,
            )
            if not result.success:
                return {"success": False, "error": result.error or "Failed to delete git worktree"}
        elif not worktree_exists:
            # Worktree path gone (manually deleted) - just clean up DB record
            logger.info(
                f"Worktree path {worktree.worktree_path} doesn't exist, cleaning up DB record only"
            )

        # Delete database record
        deleted = worktree_storage.delete(worktree_id)
        if not deleted:
            return {"success": False, "error": "Failed to delete worktree record"}

        return {"success": True}

    @registry.tool(
        name="sync_worktree",
        description="Sync a worktree with the main branch.",
    )
    async def sync_worktree(
        worktree_id: str,
        strategy: str = "merge",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Sync a worktree with the main branch.

        Args:
            worktree_id: The worktree ID to sync.
            strategy: Sync strategy ('merge' or 'rebase').
            project_path: Path to project directory (pass cwd from CLI).

        Returns:
            Dict with sync result.
        """
        # Resolve git manager from project_path or fall back to default
        resolved_git_mgr, _, error = _resolve_project_context(project_path, git_manager, project_id)
        if error:
            return {"success": False, "error": error}

        if resolved_git_mgr is None:
            return {
                "success": False,
                "error": "Git manager not configured and no project_path provided.",
            }

        worktree = worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        # Validate strategy
        if strategy not in ("rebase", "merge"):
            return {
                "success": False,
                "error": f"Invalid strategy '{strategy}'. Must be 'rebase' or 'merge'.",
            }

        strategy_literal = cast(Literal["rebase", "merge"], strategy)

        result = resolved_git_mgr.sync_from_main(
            worktree.worktree_path,
            base_branch=worktree.base_branch,
            strategy=strategy_literal,
        )

        if not result.success:
            return {"success": False, "error": result.error or "Sync failed"}

        # Update last activity
        worktree_storage.update(worktree_id)

        return {
            "success": True,
            "message": result.message,
            "output": result.output,
            "strategy": strategy,
        }

    @registry.tool(
        name="mark_worktree_merged",
        description="Mark a worktree as merged (ready for cleanup).",
    )
    async def mark_worktree_merged(worktree_id: str) -> dict[str, Any]:
        """
        Mark a worktree as merged.

        Args:
            worktree_id: The worktree ID to mark.

        Returns:
            Dict with success status.
        """
        worktree = worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = worktree_storage.mark_merged(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to mark worktree as merged"}

        return {"success": True}

    @registry.tool(
        name="detect_stale_worktrees",
        description="Find worktrees with no activity for a period.",
    )
    async def detect_stale_worktrees(
        project_path: str | None = None,
        hours: int | str = 24,
        limit: int | str = 50,
    ) -> dict[str, Any]:
        """
        Find stale worktrees (no activity for N hours).

        Args:
            project_path: Path to project directory (pass cwd from CLI).
            hours: Hours of inactivity threshold (default: 24).
            limit: Maximum results (default: 50).

        Returns:
            Dict with list of stale worktrees.
        """
        # Handle string inputs from MCP (JSON params come as strings)
        hours = int(hours) if isinstance(hours, str) else hours
        limit = int(limit) if isinstance(limit, str) else limit

        _, resolved_project_id, error = _resolve_project_context(
            project_path, git_manager, project_id
        )
        if error:
            return {"success": False, "error": error}
        if resolved_project_id is None:
            return {"success": False, "error": "Could not resolve project ID"}

        stale = worktree_storage.find_stale(
            project_id=resolved_project_id,
            hours=hours,
            limit=limit,
        )

        return {
            "success": True,
            "stale_worktrees": [
                {
                    "id": wt.id,
                    "branch_name": wt.branch_name,
                    "worktree_path": wt.worktree_path,
                    "updated_at": wt.updated_at,
                    "task_id": wt.task_id,
                }
                for wt in stale
            ],
            "count": len(stale),
            "threshold_hours": hours,
        }

    @registry.tool(
        name="cleanup_stale_worktrees",
        description="Mark and optionally delete stale worktrees.",
    )
    async def cleanup_stale_worktrees(
        project_path: str | None = None,
        hours: int | str = 24,
        dry_run: bool | str = True,
        delete_git: bool | str = False,
    ) -> dict[str, Any]:
        """
        Cleanup stale worktrees.

        Args:
            project_path: Path to project directory (pass cwd from CLI).
            hours: Hours of inactivity threshold (default: 24).
            dry_run: If True, only report what would be cleaned (default: True).
            delete_git: If True, also delete git worktrees (default: False).

        Returns:
            Dict with cleanup results.
        """
        # Handle string inputs from MCP (JSON params come as strings)
        hours = int(hours) if isinstance(hours, str) else hours
        dry_run = dry_run in (True, "true", "True", "1") if isinstance(dry_run, str) else dry_run
        delete_git = (
            delete_git in (True, "true", "True", "1") if isinstance(delete_git, str) else delete_git
        )

        resolved_git_manager, resolved_project_id, error = _resolve_project_context(
            project_path, git_manager, project_id
        )
        if error:
            return {"success": False, "error": error}
        if resolved_project_id is None:
            return {"success": False, "error": "Could not resolve project ID"}

        # Find and mark stale worktrees
        stale = worktree_storage.cleanup_stale(
            project_id=resolved_project_id,
            hours=hours,
            dry_run=dry_run,
        )

        results = []
        for wt in stale:
            result = {
                "id": wt.id,
                "branch_name": wt.branch_name,
                "worktree_path": wt.worktree_path,
                "marked_abandoned": not dry_run,
                "git_deleted": False,
            }

            # Optionally delete git worktrees
            if delete_git and not dry_run and resolved_git_manager:
                git_result = resolved_git_manager.delete_worktree(
                    wt.worktree_path,
                    force=True,
                    delete_branch=True,
                    branch_name=wt.branch_name,
                )
                result["git_deleted"] = git_result.success
                if not git_result.success:
                    result["git_error"] = git_result.error or "Unknown error"

            results.append(result)

        return {
            "success": True,
            "dry_run": dry_run,
            "cleaned": results,
            "count": len(results),
            "threshold_hours": hours,
        }

    @registry.tool(
        name="get_worktree_stats",
        description="Get worktree statistics for the project.",
    )
    async def get_worktree_stats(project_path: str | None = None) -> dict[str, Any]:
        """
        Get worktree statistics.

        Args:
            project_path: Path to project directory (pass cwd from CLI).

        Returns:
            Dict with counts by status.
        """
        # Resolve project context (git_manager not needed for stats)
        _, resolved_project_id, error = _resolve_project_context(
            project_path, git_manager, project_id
        )
        if error:
            return {"success": False, "error": error}

        # Type narrowing: if no error, resolved_project_id is guaranteed non-None
        if resolved_project_id is None:
            raise RuntimeError("Project ID unexpectedly None")

        counts = worktree_storage.count_by_status(resolved_project_id)

        return {
            "success": True,
            "project_id": resolved_project_id,
            "counts": counts,
            "total": sum(counts.values()),
        }

    @registry.tool(
        name="get_worktree_by_task",
        description="Get worktree linked to a specific task.",
    )
    async def get_worktree_by_task(task_id: str) -> dict[str, Any]:
        """
        Get worktree linked to a task.

        Args:
            task_id: The task ID to look up.

        Returns:
            Dict with worktree details or not found.
        """
        worktree = worktree_storage.get_by_task(task_id)
        if not worktree:
            return {"success": False, "error": f"No worktree linked to task '{task_id}'"}

        return {"success": True, "worktree": worktree.to_dict()}

    @registry.tool(
        name="link_task_to_worktree",
        description="Link a task to an existing worktree.",
    )
    async def link_task_to_worktree(
        worktree_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """
        Link a task to a worktree.

        Args:
            worktree_id: The worktree ID.
            task_id: The task ID to link.

        Returns:
            Dict with success status.
        """
        worktree = worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = worktree_storage.update(worktree_id, task_id=task_id)
        if not updated:
            return {"success": False, "error": "Failed to link task to worktree"}

        return {"success": True}

    @registry.tool(
        name="merge_worktree",
        description="Merge a worktree's branch into its base branch (or a specified target).",
    )
    async def merge_worktree(
        worktree_id: str,
        source_branch: str | None = None,
        target_branch: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Merge and push from worktree — fully isolated, never touches main repo.

        1. Fetch latest in the worktree
        2. Merge target INTO source branch (in worktree — conflicts resolved here)
        3. Push source to origin as target (git push origin source:target)

        The main repo is never touched. All operations use cwd=worktree_path.

        Args:
            worktree_id: The worktree ID to merge.
            source_branch: Agent's working branch (defaults to worktree's branch_name).
            target_branch: Branch to merge into (defaults to worktree's base_branch).
            project_path: Path to project directory (pass cwd from CLI).

        Returns:
            Dict with source_branch, target_branch on success.
        """

        resolved_git_mgr, _, error = _resolve_project_context(project_path, git_manager, project_id)
        if error:
            return {"success": False, "error": error}
        if resolved_git_mgr is None:
            return {"success": False, "error": "Git manager not available"}

        worktree = worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        effective_source = source_branch or worktree.branch_name
        merge_target = target_branch or worktree.base_branch
        wt_path = worktree.worktree_path

        # Step 1: Fetch latest in the worktree
        fetch_result = resolved_git_mgr._run_git(["fetch", "origin"], cwd=wt_path, timeout=60)
        if fetch_result.returncode != 0:
            logger.warning(f"Fetch failed in worktree (non-fatal): {fetch_result.stderr.strip()}")

        # Step 2: Merge target INTO source branch (in worktree)
        # Use origin/ prefix to merge the freshly-fetched remote ref, not a stale local branch
        merge_ref = (
            f"origin/{merge_target}" if not merge_target.startswith("origin/") else merge_target
        )
        merge_result = resolved_git_mgr._run_git(
            ["merge", merge_ref, "--no-edit"],
            cwd=wt_path,
            timeout=60,
        )

        if merge_result.returncode != 0:
            merge_output = merge_result.stdout + merge_result.stderr
            if "CONFLICT" in merge_output:
                resolved_git_mgr._run_git(["merge", "--abort"], cwd=wt_path, timeout=10)
                conflict_lines = [
                    line.strip() for line in merge_output.split("\n") if "CONFLICT" in line
                ]
                return {
                    "success": False,
                    "has_conflicts": True,
                    "conflicted_files": conflict_lines,
                    "error": "merge_conflict",
                    "message": (
                        f"Merge conflicts detected in {len(conflict_lines)} file(s). "
                        "Use gobby-merge tools to resolve."
                    ),
                }
            return {
                "success": False,
                "has_conflicts": False,
                "error": merge_output.strip(),
            }

        # Mark as merged in storage
        worktree_storage.mark_merged(worktree_id)

        return {
            "success": True,
            "message": (
                f"Merged origin/{merge_target} into {effective_source} in worktree. "
                f"Run: git push --no-verify origin {effective_source}:{merge_target}"
            ),
            "source_branch": effective_source,
            "target_branch": merge_target,
            "push_command": f"git push --no-verify origin {effective_source}:{merge_target}",
        }

    return registry
