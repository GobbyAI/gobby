"""
Utilities for resolving project context.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import stat
import subprocess  # nosec B404 # git argv is a fixed isolation restore command.
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.utils.env import is_test_protect_enabled

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

ISOLATION_MARKER_RELATIVE_PATH = ".gobby/isolation.json"
PROJECT_JSON_RELATIVE_PATH = ".gobby/project.json"
_PARENT_PROJECT_PATH_KEY = "parent_project_path"
_PARENT_PROJECT_ID_KEY = "parent_project_id"
_PARENT_KEYS = (_PARENT_PROJECT_PATH_KEY, _PARENT_PROJECT_ID_KEY)


def set_project_context(ctx: dict[str, Any] | None) -> contextvars.Token[dict[str, Any] | None]:
    """Set project context for the current async task (used by MCP tool calls).

    Blocks known test project IDs in production to prevent e2e test leakage.
    Set GOBBY_TEST_PROTECT=1 in test environments to allow test IDs.
    """
    if ctx is not None and not is_test_protect_enabled():
        pid = ctx.get("id", "")
        if isinstance(pid, str) and pid in _TEST_PROJECT_IDS:
            logger.warning("Blocked test project_id '%s' in production context", pid)
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
        - parent_project_path: Optional path to parent project for isolated roots
        - parent_project_id: Optional parent logical project ID for isolated roots
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

    # 2. An explicit cwd is authoritative. A daemon or web-chat subprocess may
    # inherit GOBBY_PROJECT_ID from its launcher, but that process-wide value
    # must not override the project identified by a caller-provided path.
    if cwd is not None:
        root = find_project_root(cwd)
        if root:
            try:
                with open(root / ".gobby" / "project.json") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise json.JSONDecodeError("project.json is not an object", "", 0)
                data = dict(loaded)
                data["project_path"] = str(root)
                return _with_isolation_marker(data, root)
            except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read project context: %s", e)

    # 3. Environment fallback (set by web chat subprocess for correct project routing)
    if cwd is None:
        override_id = os.environ.get("GOBBY_PROJECT_ID")
        if override_id:
            return {"id": override_id}

    # Only search the filesystem when an explicit cwd was provided. When no
    # cwd project or environment fallback exists, resolution is unavailable.
    # When cwd is None, the caller is in daemon context where os.getcwd()
    # points to the daemon's directory, NOT the calling session's project.
    # The stdio proxy injects the correct project via HTTP headers instead.
    return None


def _build_and_set_project_context(
    project: Any,
    db: Any | None = None,
    machine_id: str | None = None,
) -> contextvars.Token[dict[str, Any] | None]:
    """Build enriched context dict from a Project checkout and set the ContextVar.

    Args:
        project: A Project dataclass instance (from storage.projects).
        db: Hub database used to resolve the calling-machine checkout.
        machine_id: Session machine id when present; otherwise the local daemon.

    Returns:
        Context var token for reset (via reset_project_context).
    """
    from gobby.storage.project_checkouts import require_root
    from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
    from gobby.storage.workspace_machine_scope import require_local_machine_id

    repo_path: str | None = None
    if db is not None and project.id not in CHECKOUT_FREE_PROJECT_IDS:
        resolved_machine = require_local_machine_id(
            machine_id, resource_kind="project_checkout", resource_id=project.id
        )
        repo_path = require_root(db, project.id, resolved_machine)
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
                        "Project ID mismatch: db='%s', filesystem='%s' at %s. Using filesystem.",
                        project.id,
                        fs_id,
                        repo_path,
                    )
                data["project_path"] = repo_path
                return set_project_context(_with_isolation_marker(data, Path(repo_path)))
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("Failed to read project.json at %s: %s", project_file, e)
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
        session_manager: SessionManager instance.
        db: Database connection for project lookup.

    Returns:
        Context var token for reset (via reset_project_context), or None.
    """
    session = session_manager.get(session_id)
    if not session or not session.project_id:
        return None

    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

    try:
        from gobby.storage.projects import LocalProjectManager

        pm = LocalProjectManager(db)
        project = pm.get(session.project_id)
        if project:
            return _build_and_set_project_context(
                project,
                db=db,
                machine_id=getattr(session, "machine_id", None),
            )
    except (ImportError, OSError, CheckoutNotFoundError, MachineOwnershipMismatchError) as e:
        # No local checkout (or a foreign-machine session) degrades to an
        # id-only context instead of failing the caller.
        logger.debug("Failed to enrich project context for session %s: %s", session_id, e)

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
    return _build_and_set_project_context(project, db=db)


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


class IsolationProjectJsonError(RuntimeError):
    """Raised when isolated project metadata cannot be created safely."""


def read_isolation_marker(root: Path) -> dict[str, str] | None:
    """Read parent isolation fields from the gitignored sidecar only."""
    path = Path(root) / ISOLATION_MARKER_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    parent_path = payload.get(_PARENT_PROJECT_PATH_KEY)
    parent_id = payload.get(_PARENT_PROJECT_ID_KEY)
    if not isinstance(parent_path, str) or not parent_path:
        return None
    if not isinstance(parent_id, str) or not parent_id:
        return None
    return {
        _PARENT_PROJECT_PATH_KEY: parent_path,
        _PARENT_PROJECT_ID_KEY: parent_id,
    }


def _with_isolation_marker(data: dict[str, Any], root: Path) -> dict[str, Any]:
    """Drop tracked parent keys and merge the sidecar when both fields exist."""
    for key in _PARENT_KEYS:
        data.pop(key, None)
    marker = read_isolation_marker(root)
    if marker is not None:
        data.update(marker)
    return data


def _atomic_write_bytes(path: Path, payload: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(temp_fd, "wb") as temp_file:
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            if mode is not None:
                os.fchmod(temp_file.fileno(), stat.S_IMODE(mode))
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove temporary file %s", temp_path)


def _restore_generated_tracked_project_json(
    isolated_path: Path,
    main_repo_path: Path,
) -> None:
    from gobby.agents.isolation_git_hygiene import is_generated_isolation_project_json

    target = isolated_path / PROJECT_JSON_RELATIVE_PATH
    if not is_generated_isolation_project_json(target, main_repo_path=main_repo_path):
        return
    checkout = subprocess.run(
        ["git", "checkout", "HEAD", "--", PROJECT_JSON_RELATIVE_PATH],
        cwd=isolated_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if checkout.returncode != 0:
        logger.warning(
            "Failed to restore generated project metadata in %s: %s",
            isolated_path,
            checkout.stderr.strip(),
        )
        return
    subprocess.run(
        ["git", "update-index", "--no-skip-worktree", "--", PROJECT_JSON_RELATIVE_PATH],
        cwd=isolated_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def ensure_project_json_for_isolation(
    source_repo_path: str | Path,
    isolated_path: str | Path,
) -> None:
    """Write the isolation sidecar without rewriting tracked project metadata.

    Copies source project.json bytes into the isolated root only when that file
    is missing. Parent identity lives in the gitignored sidecar so git status
    stays clean. Generated parent-key dirt on tracked metadata is restored from
    HEAD when it matches Gobby's old rewrite.
    """
    source_root = Path(source_repo_path)
    isolated_root = Path(isolated_path)
    source_project_json = source_root / PROJECT_JSON_RELATIVE_PATH

    if not source_project_json.exists():
        return

    try:
        source_bytes = source_project_json.read_bytes()
        source_data = json.loads(source_bytes)
        parent_project_id = source_data["id"]
        if not isinstance(parent_project_id, str) or not parent_project_id:
            raise KeyError("id")

        marker = {
            _PARENT_PROJECT_PATH_KEY: str(source_root.resolve()),
            _PARENT_PROJECT_ID_KEY: parent_project_id,
        }
        marker_bytes = (json.dumps(marker, indent=2) + "\n").encode()

        target_project_json = isolated_root / PROJECT_JSON_RELATIVE_PATH
        if not target_project_json.exists():
            _atomic_write_bytes(
                target_project_json,
                source_bytes,
                mode=source_project_json.stat().st_mode,
            )

        _atomic_write_bytes(isolated_root / ISOLATION_MARKER_RELATIVE_PATH, marker_bytes)
        _restore_generated_tracked_project_json(isolated_root, source_root)
        logger.info("Wrote isolation sidecar in %s", isolated_root)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        raise IsolationProjectJsonError(
            f"Failed to write isolation marker in isolated environment {isolated_path}"
        ) from exc


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
        logger.warning("Failed to parse verification config: %s", e)
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
        logger.warning("Failed to parse hooks config: %s", e)
        return None
