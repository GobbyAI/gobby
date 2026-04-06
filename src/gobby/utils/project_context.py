"""
Utilities for resolving project context.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from gobby.config.features import HooksConfig, ProjectVerificationConfig

logger = logging.getLogger(__name__)

# Per-async-task project context, set by MCP tool calls from session data.
# Checked first by get_project_context() so daemon-level tools resolve the
# calling session's project, not the daemon's cwd.
_current_project_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "current_project_context", default=None
)


_TEST_PROJECT_IDS = frozenset({"e2e-test-project", "test-project"})


def set_project_context(ctx: dict[str, Any] | None) -> contextvars.Token[dict[str, Any] | None]:
    """Set project context for the current async task (used by MCP tool calls).

    Blocks known test project IDs in production to prevent e2e test leakage.
    Set GOBBY_TEST_PROTECT=1 in test environments to allow test IDs.
    """
    if ctx is not None and not os.environ.get("GOBBY_TEST_PROTECT"):
        pid = ctx.get("id", "")
        if isinstance(pid, str) and pid in _TEST_PROJECT_IDS:
            logger.warning(f"Blocked test project_id '{pid}' in production context")
            return _current_project_context.set(None)
    return _current_project_context.set(ctx)


def reset_project_context(token: contextvars.Token[dict[str, Any] | None]) -> None:
    """Reset project context after tool call completes."""
    _current_project_context.reset(token)


def find_project_root(cwd: Path | None = None) -> Path | None:
    """
    Find the project root directory by looking for .gobby/project.json.

    Args:
        cwd: Current working directory to start search from. Defaults to Path.cwd().

    Returns:
        Path to project root if found, None otherwise.
    """
    if cwd is None:
        cwd = Path.cwd()

    current = cwd.resolve()
    # Traverse up
    for parent in [current] + list(current.parents):
        project_file = parent / ".gobby" / "project.json"
        if project_file.exists():
            return parent
    return None


def get_project_context(cwd: Path | None = None) -> dict[str, Any] | None:
    """
    Get project context from .gobby/project.json.

    Args:
        cwd: Current working directory to start search from.

    Returns:
        Dictionary containing project data (id, name, verification, etc.) and 'project_path',
        or None if not found.

        The returned dict may include:
        - id: Project ID
        - name: Project name
        - created_at: Creation timestamp
        - project_path: Path to project root
        - verification: Optional dict with unit_tests, type_check, lint, integration, custom
    """
    # 1. Check context var (set per-MCP-call from session), but only when
    # no explicit cwd was provided. Callers passing cwd want filesystem
    # resolution; the context var is for MCP tool handlers that don't know
    # their cwd.
    if cwd is None:
        ctx = _current_project_context.get()
        if ctx is not None:
            return ctx

    # 2. Environment override (set by web chat subprocess for correct project routing)
    override_id = os.environ.get("GOBBY_PROJECT_ID")
    if override_id:
        root = find_project_root(cwd)
        if root:
            try:
                with open(root / ".gobby" / "project.json") as f:
                    data = json.load(f)
                data["project_path"] = str(root)
                if data.get("id") == override_id:
                    return cast(dict[str, Any], data)
            except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError) as e:
                logger.debug(f"Failed to read project.json for override ID {override_id}: {e}")
        # CWD doesn't match — return minimal context with just the ID
        return {"id": override_id}

    # Only search the filesystem when an explicit cwd was provided.
    # When cwd is None, the caller is in daemon context where os.getcwd()
    # points to the daemon's directory, NOT the calling session's project.
    # The stdio proxy injects the correct project via HTTP headers instead.
    if cwd is None:
        return None

    root = find_project_root(cwd)
    if not root:
        return None

    project_file = root / ".gobby" / "project.json"
    try:
        with open(project_file) as f:
            data = json.load(f)
        data["project_path"] = str(root)
        return cast(dict[str, Any], data)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read project context: {e}")
        return None


def _build_and_set_project_context(
    project: Any,
) -> contextvars.Token[dict[str, Any] | None]:
    """Build enriched context dict from a Project and set the ContextVar.

    Normalizes repo_path, builds base context, and enriches from
    .gobby/project.json on disk when available.

    Args:
        project: A Project dataclass instance (from storage.projects).

    Returns:
        Context var token for reset (via reset_project_context).
    """
    # Normalize empty/whitespace repo_path to None — system projects
    # (_global, _personal, _orphaned, _migrated) store "" in the DB.
    repo_path = project.repo_path
    if repo_path is not None and not repo_path.strip():
        repo_path = None
    ctx: dict[str, Any] = {
        "id": project.id,
        "name": project.name,
        "project_path": repo_path,
    }
    if repo_path:
        project_file = Path(repo_path) / ".gobby" / "project.json"
        if project_file.exists():
            try:
                data = json.loads(project_file.read_text())
                fs_id = data.get("id")
                if fs_id and fs_id != project.id:
                    logger.warning(
                        f"Project ID mismatch: db='{project.id}', "
                        f"filesystem='{fs_id}' at {project.repo_path}. Using filesystem.",
                    )
                data["project_path"] = project.repo_path
                return set_project_context(data)
            except (json.JSONDecodeError, OSError) as e:
                logger.debug(f"Failed to read project.json at {project_file}: {e}")
    return set_project_context(ctx)


def set_project_context_from_session(
    session_id: str,
    session_manager: Any,
    db: Any,
) -> contextvars.Token[dict[str, Any] | None] | None:
    """Look up session's project and set project context var.

    Shared utility for any dispatch path that needs to set the project
    context from a session_id. Used by MCPProxyServer.call_tool (rules engine
    path) and ToolProxyService.call_tool (HTTP dispatch path).

    Args:
        session_id: Resolved session UUID.
        session_manager: LocalSessionManager instance.
        db: Database connection for project lookup.

    Returns:
        Context var token for reset (via reset_project_context), or None.
    """
    session = session_manager.get(session_id)
    if not session or not session.project_id:
        return None

    try:
        from gobby.storage.projects import LocalProjectManager

        pm = LocalProjectManager(db)
        project = pm.get(session.project_id)
        if project:
            return _build_and_set_project_context(project)
    except (ImportError, OSError) as e:
        logger.debug(f"Failed to enrich project context for session {session_id}: {e}")

    return set_project_context({"id": session.project_id})


def set_project_context_from_ref(
    ref: str,
    db: Any,
) -> contextvars.Token[dict[str, Any] | None] | None:
    """Resolve a project by UUID or name and set project context var.

    Used by call_tool's project_id parameter to override session-derived
    project context for cross-project operations.

    Args:
        ref: Project UUID or name (resolved via LocalProjectManager.resolve_ref).
        db: Database connection for project lookup.

    Returns:
        Context var token for reset (via reset_project_context), or None
        if the project was not found.
    """
    from gobby.storage.projects import LocalProjectManager

    pm = LocalProjectManager(db)
    project = pm.resolve_ref(ref)
    if not project:
        return None
    return _build_and_set_project_context(project)


def get_workflow_project_path(cwd: Path | None = None) -> Path | None:
    """
    Get the project path for workflow lookup.

    In a worktree, returns parent_project_path (where workflows live).
    In a main project, returns the project_path.

    This allows worktree agents to discover workflows from the parent project
    without needing to explicitly pass the project_path parameter.

    Args:
        cwd: Current working directory to start search from.

    Returns:
        Path to use for workflow discovery, or None if no project found.
    """
    ctx = get_project_context(cwd)
    if not ctx:
        return None

    # If in a worktree, use parent project for workflows
    parent = ctx.get("parent_project_path")
    if parent:
        return Path(parent)

    # Otherwise use current project path
    project_path = ctx.get("project_path")
    return Path(project_path) if project_path else None


def ensure_project_json_for_isolation(
    source_repo_path: str | Path,
    isolated_path: str | Path,
) -> None:
    """
    Ensure .gobby/project.json exists in an isolated environment with parent_project_path.

    Reads project.json from the source repo and writes it to the isolated path
    (worktree or clone) with a ``parent_project_path`` field pointing back to
    the source. This allows ``get_workflow_project_path()`` to discover
    workflows from the parent project.

    Always overwrites any existing project.json in the target — git-tracked
    copies won't have the ``parent_project_path`` field.

    Args:
        source_repo_path: Path to the main/source repository.
        isolated_path: Path to the worktree or clone directory.
    """
    source_project_json = Path(source_repo_path) / ".gobby" / "project.json"

    if not source_project_json.exists():
        return

    try:
        with open(source_project_json) as f:
            data = json.load(f)

        data["parent_project_path"] = str(Path(source_repo_path).resolve())

        target_gobby_dir = Path(isolated_path) / ".gobby"
        target_gobby_dir.mkdir(parents=True, exist_ok=True)

        with open(target_gobby_dir / "project.json", "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Wrote project.json with parent reference in {isolated_path}")
    except Exception as e:
        logger.warning(f"Failed to write project.json in {isolated_path}: {e}")


def get_project_mcp_dir(project_name: str) -> Path:
    """
    Get the directory for project-specific MCP configuration.

    Args:
        project_name: Name of the project.

    Returns:
        Path to the project's MCP directory in ~/.gobby/projects/.
    """
    project_name_safe = project_name.replace(" ", "_").lower()
    return Path.home() / ".gobby" / "projects" / project_name_safe


def get_project_mcp_config_path(project_name: str) -> Path:
    """
    Get the path to the project-specific .mcp.json file.

    Args:
        project_name: Name of the project.

    Returns:
        Path to .mcp.json.
    """
    return get_project_mcp_dir(project_name) / ".mcp.json"


def get_verification_config(cwd: Path | None = None) -> ProjectVerificationConfig | None:
    """
    Get project verification configuration from .gobby/project.json.

    Args:
        cwd: Current working directory to start search from.

    Returns:
        ProjectVerificationConfig if verification section exists, None otherwise.
    """
    from gobby.config.features import ProjectVerificationConfig

    context = get_project_context(cwd)
    if not context:
        return None

    verification_data = context.get("verification")
    if not verification_data:
        return None

    try:
        return ProjectVerificationConfig(**verification_data)
    except Exception as e:
        logger.warning(f"Failed to parse verification config: {e}")
        return None


def get_hooks_config(cwd: Path | None = None) -> HooksConfig | None:
    """
    Get git hooks configuration from .gobby/project.json.

    Args:
        cwd: Current working directory to start search from.

    Returns:
        HooksConfig if hooks section exists, None otherwise.
    """
    from gobby.config.features import HooksConfig

    context = get_project_context(cwd)
    if not context:
        return None

    hooks_data = context.get("hooks")
    if not hooks_data:
        return None

    try:
        return HooksConfig(**hooks_data)
    except Exception as e:
        logger.warning(f"Failed to parse hooks config: {e}")
        return None
