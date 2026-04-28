"""Shared helpers for worktree tools.

Module-level utility functions used across worktree tool handlers.
"""

from __future__ import annotations

import importlib
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from gobby.utils.project_context import ensure_project_json_for_isolation, get_project_context
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def _get_cached_git_manager(path: str) -> WorktreeGitManager:
    """Get or create a cached WorktreeGitManager for a repo path.

    Thread-safe via lru_cache's internal lock. Raises ValueError
    if the path is not a valid git repository (not cached on error).
    """
    return WorktreeGitManager(path)


def get_worktree_base_dir() -> Path:
    """Get the base directory for worktrees.

    Uses ~/.gobby/worktrees/ so worktrees survive reboots.

    Returns:
        Path to worktree base directory (creates if needed)
    """
    base = Path.home() / ".gobby" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    return base


def generate_worktree_path(branch_name: str, project_name: str | None = None) -> str:
    """Generate a worktree path in the base directory.

    Args:
        branch_name: Branch name (used as directory name)
        project_name: Optional project name for namespacing

    Returns:
        Full path for the worktree
    """
    base = get_worktree_base_dir()
    safe_branch = re.sub(r'[\\:*?"<>|\s/]', "-", branch_name)
    safe_branch = re.sub(r"-{2,}", "-", safe_branch).strip("-")
    if not safe_branch:
        safe_branch = "unnamed-branch"

    if project_name:
        return str(base / project_name / safe_branch)
    else:
        return str(base / safe_branch)


def resolve_project_context(
    project_path: str | None,
    default_git_manager: WorktreeGitManager | None,
    default_project_id: str | None,
) -> tuple[WorktreeGitManager | None, str | None, str | None]:
    """Resolve project context from project_path or fall back to defaults.

    Args:
        project_path: Path to project directory (cwd from caller).
        default_git_manager: Registry-level git manager (may be None).
        default_project_id: Registry-level project ID (may be None).

    Returns:
        Tuple of (git_manager, project_id, error_message).
        If error_message is not None, the other values should not be used.
    """
    if project_path:
        path = Path(project_path)
        if not path.exists():
            return None, None, f"Project path does not exist: {project_path}"

        project_ctx = get_project_context(path)
        if not project_ctx:
            return None, None, f"No .gobby/project.json found in {project_path}"

        resolved_project_id = project_ctx.get("id")
        resolved_path = project_ctx.get("project_path", str(path))

        try:
            git_mgr = _get_cached_git_manager(resolved_path)
        except ValueError as e:
            return None, None, f"Invalid git repository: {e}"

        return git_mgr, resolved_project_id, None

    # Fall back to defaults
    if default_git_manager is not None and default_project_id is not None:
        return default_git_manager, default_project_id, None

    # Try resolving from context var (set per-MCP-call from session)
    ctx = get_project_context()
    if ctx and ctx.get("id"):
        resolved_path = ctx.get("project_path")
        if resolved_path:
            try:
                git_mgr = _get_cached_git_manager(resolved_path)
            except ValueError as e:
                return None, None, f"Failed to initialize git manager for {resolved_path}: {e}"
            return git_mgr, ctx["id"], None

    return (
        None,
        None,
        "No project_path provided and no project context available. Pass project_path parameter.",
    )


def copy_project_json_to_worktree(
    repo_path: str | Path,
    worktree_path: str | Path,
) -> None:
    """Copy .gobby/project.json from main repo to worktree, adding parent reference.

    Delegates to ``ensure_project_json_for_isolation`` which always writes
    ``parent_project_path`` even when the file already exists (e.g. git-tracked).
    """
    ensure_project_json_for_isolation(repo_path, worktree_path)


# Registry: (module_path, function_name, uses_mode_param)
_PROVIDER_INSTALLERS: dict[str, tuple[str, str, bool]] = {
    "claude": ("gobby.cli.installers.claude", "install_claude", True),
    "gemini": ("gobby.cli.installers.gemini", "install_gemini", False),
    "qwen": ("gobby.cli.installers.qwen", "install_qwen", True),
    "droid": ("gobby.cli.installers.droid", "install_droid", True),
    # Note: codex uses CODEX_NOTIFY_SCRIPT env var, not project-level hooks
}


def install_provider_hooks(
    provider: Literal["claude", "gemini", "qwen", "codex", "droid"] | None,
    worktree_path: str | Path,
) -> bool:
    """Install CLI hooks for the specified provider in the worktree.

    Note: "codex" is accepted for type consistency but always returns False.
    Codex uses the CODEX_NOTIFY_SCRIPT env var for hook integration rather
    than project-level hooks. See ``_PROVIDER_INSTALLERS`` for supported providers.

    Args:
        provider: Provider name or None
        worktree_path: Path to worktree directory

    Returns:
        True if hooks were successfully installed, False otherwise
    """
    if not provider:
        return False

    entry = _PROVIDER_INSTALLERS.get(provider)
    if not entry:
        return False

    module_path, func_name, uses_mode = entry
    worktree_path_obj = Path(worktree_path)
    try:
        mod = importlib.import_module(module_path)
        installer = getattr(mod, func_name)
        result = (
            installer(worktree_path_obj, mode="project")
            if uses_mode
            else installer(worktree_path_obj)
        )
        if result["success"]:
            logger.info(f"Installed {provider} hooks in worktree: {worktree_path}")
            return True
        else:
            logger.warning(f"Failed to install {provider} hooks: {result.get('error')}")
    except Exception as e:
        logger.warning(f"Failed to install {provider} hooks in worktree: {e}")
    return False
