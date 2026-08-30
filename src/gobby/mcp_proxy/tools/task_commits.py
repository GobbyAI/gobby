"""
Task commit linking MCP tools module.

Provides tools for linking git commits to tasks:
- link_commit: Link a git commit to a task
- unlink_commit: Unlink a git commit from a task
- auto_link_commits: Auto-detect and link commits mentioning task IDs
- get_task_diff: Page a task's commit and working-tree diff

Extracted from tasks.py using Strangler Fig pattern for code decomposition.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    resolve_project_repo_path,
    resolve_task_repo_path,
)
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.workspace_machine_scope import require_local_machine_id
from gobby.tasks.diff_paging import (
    DEFAULT_GIT_TIMEOUT_SECONDS,
    MAX_COMMITS_LIMIT,
    MAX_CURSOR_OFFSET,
    MAX_LIMIT_BYTES,
    MAX_MANIFEST_LIMIT,
    MIN_LIMIT_BYTES,
    DiffPage,
    DiffPagingError,
)
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

__all__ = ["create_commit_registry"]


def get_current_project_id() -> str | None:
    """Get the current project ID from context."""
    context = get_project_context()
    return context.get("id") if context else None


def _session_machine_id(session_manager: Any | None, project_id: str | None) -> str | None:
    if not project_id:
        return None
    session_ref = get_current_session_id()
    if session_ref and session_manager is not None:
        try:
            resolved = session_manager.resolve_session_reference(session_ref, project_id)
            session = session_manager.get(resolved)
        except (ValueError, KeyError, LookupError):
            session = None
        if session is None or not session.machine_id:
            from gobby.storage.project_checkouts import MissingMachineContextError

            raise MissingMachineContextError(
                f"session {session_ref} has no machine_id for project checkout"
            )
        return require_local_machine_id(
            session.machine_id, resource_kind="project_checkout", resource_id=project_id
        )
    return require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)


def create_commit_registry(
    task_manager: "LocalTaskManager | None" = None,
    project_manager: "LocalProjectManager | None" = None,
    auto_link_commits_fn: Callable[..., Any] | None = None,
    get_task_diff_page_fn: Callable[..., DiffPage] | None = None,
    git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    session_manager: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a registry with commit linking tools.

    Args:
        task_manager: LocalTaskManager instance (required for task ID resolution)
        project_manager: LocalProjectManager instance (for repo_path lookup)
        auto_link_commits_fn: Function for auto-linking commits (injectable for testing)
        get_task_diff_page_fn: Function for paging task diffs (injectable for testing)
        git_timeout_seconds: Server-owned deadline for each git subprocess
        session_manager: Session manager (unused, kept for interface compat)

    Returns:
        InternalToolRegistry with commit linking tools registered
    """
    # Lazy import to avoid circular dependency
    from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp
    from gobby.mcp_proxy.tools.tasks._authorization import require_claim_authority

    registry = InternalToolRegistry(
        name="gobby-tasks-commits",
        description="Task commit linking tools",
    )

    if task_manager is None:
        raise ValueError("task_manager is required for task ID resolution")

    def _get_task_and_repo_path(
        resolved_task_id: str,
        task_id: str,
        project_path: str | None,
    ) -> tuple[Any, str | None] | dict[str, str]:
        task = task_manager.get_task(resolved_task_id)
        if task is None:
            return {"error": f"Task {task_id} not found"}
        try:
            repo_path = resolve_task_repo_path(
                task_manager=task_manager,
                project_manager=project_manager,
                task=task,
                project_path=project_path,
                machine_id=_session_machine_id(session_manager, task.project_id),
            )
        except RepoPathValidationError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}
        return task, repo_path

    # --- link_commit ---

    def link_commit(
        task_id: str,
        commit_sha: str,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Link a git commit to a task."""
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        task_and_repo_path = _get_task_and_repo_path(resolved_task_id, task_id, project_path)
        if isinstance(task_and_repo_path, dict):
            return task_and_repo_path
        current_task, repo_path = task_and_repo_path

        denied = require_claim_authority(task_manager, current_task, "link_commit")
        if denied:
            return denied

        try:
            task = task_manager.link_commit(resolved_task_id, commit_sha, cwd=repo_path)
            return {
                "task_id": task.id,
                "commits": task.commits or [],
            }
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="link_commit",
        description="Link a git commit to a task. NOTE: For closing tasks, prefer close_task(task_id, commit_sha='...') which links and closes in one call. Use link_commit only when you need to link without closing.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "commit_sha": {
                    "type": "string",
                    "description": "Git commit SHA (short or full)",
                },
                "project_path": {
                    "type": "string",
                    "description": (
                        "Repository path that contains the commit. Optional; defaults to the "
                        "current task project repository."
                    ),
                },
            },
            "required": ["task_id", "commit_sha"],
        },
        func=link_commit,
    )

    # --- unlink_commit ---

    def unlink_commit(
        task_id: str,
        commit_sha: str,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Unlink a git commit from a task."""
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        task_and_repo_path = _get_task_and_repo_path(resolved_task_id, task_id, project_path)
        if isinstance(task_and_repo_path, dict):
            return task_and_repo_path
        current_task, repo_path = task_and_repo_path

        denied = require_claim_authority(task_manager, current_task, "unlink_commit")
        if denied:
            return denied

        try:
            task = task_manager.unlink_commit(resolved_task_id, commit_sha, cwd=repo_path)
            return {
                "task_id": task.id,
                "commits": task.commits or [],
            }
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="unlink_commit",
        description="Unlink a git commit from a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "commit_sha": {
                    "type": "string",
                    "description": "Git commit SHA to unlink",
                },
                "project_path": {
                    "type": "string",
                    "description": (
                        "Repository path that contains the commit. Optional; defaults to the "
                        "current task project repository."
                    ),
                },
            },
            "required": ["task_id", "commit_sha"],
        },
        func=unlink_commit,
    )

    # --- auto_link_commits ---

    def auto_link_commits(
        task_id: str | None = None,
        since: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Auto-detect and link commits that mention task IDs."""
        if auto_link_commits_fn is None:
            return {"error": "auto_link_commits_fn not configured"}

        # Validate task exists if provided, but keep original #N format
        # because extract_task_ids_from_message returns #N format from git log
        resolved_task_id: str | None = None
        task_project_id: str | None = None
        if task_id:
            try:
                resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid task_id: {e}"}
            task_and_repo_path = _get_task_and_repo_path(resolved_task_id, task_id, project_path)
            if isinstance(task_and_repo_path, dict):
                return task_and_repo_path
            task, repo_path = task_and_repo_path
            task_project_id = task.project_id
        else:
            try:
                current_project_id = get_current_project_id()
                repo_path = resolve_project_repo_path(
                    project_manager=project_manager,
                    project_path=project_path,
                    project_id=current_project_id,
                    machine_id=_session_machine_id(session_manager, current_project_id),
                )
            except RepoPathValidationError as e:
                return {"error": str(e)}
            except ValueError as e:
                return {"error": str(e)}

        # Get project_id for resolving #N task references
        project_id = task_project_id or get_current_project_id()

        result = auto_link_commits_fn(
            task_manager=task_manager,
            task_id=task_id,  # Pass original #N format, not UUID
            since=since,
            cwd=repo_path,
            project_id=project_id,
        )

        return {
            "linked_tasks": result.linked_tasks,
            "total_linked": result.total_linked,
            "skipped": result.skipped,
            "skipped_refs": result.skipped_refs,
        }

    registry.register(
        name="auto_link_commits",
        description="Auto-detect and link commits that mention task IDs in their messages. "
        "Supports patterns: [gt-xxxxx], gt-xxxxx:, Implements/Fixes/Closes gt-xxxxx.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Filter to specific task (#N, N, path, or UUID). Optional.",
                    "default": None,
                },
                "since": {
                    "type": "string",
                    "description": "Git --since parameter (e.g., '1 week ago', '2024-01-01')",
                    "default": None,
                },
                "project_path": {
                    "type": "string",
                    "description": (
                        "Repository path to scan. Optional; defaults to the current task "
                        "project repository."
                    ),
                    "default": None,
                },
            },
        },
        func=auto_link_commits,
    )

    # --- get_task_diff ---

    def get_task_diff_tool(
        task_id: str,
        include_uncommitted: bool = False,
        project_path: str | None = None,
        commit: str | None = None,
        path_selector: str | None = None,
        offset_bytes: int = 0,
        limit_bytes: int = MAX_LIMIT_BYTES,
        commits_offset: int = 0,
        commits_limit: int = MAX_COMMITS_LIMIT,
        manifest_offset: int = 0,
        manifest_limit: int = MAX_MANIFEST_LIMIT,
        snapshot_hash: str | None = None,
        view_hash: str | None = None,
    ) -> dict[str, Any]:
        """Get one lossless page of a task diff."""
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        task_and_repo_path = _get_task_and_repo_path(resolved_task_id, task_id, project_path)
        if isinstance(task_and_repo_path, dict):
            return task_and_repo_path
        _, repo_path = task_and_repo_path

        if get_task_diff_page_fn is None:
            return {
                "success": False,
                "error_code": "not_configured",
                "error": "diff pager not configured",
            }

        try:
            return cast(
                dict[str, Any],
                get_task_diff_page_fn(
                    task_id=resolved_task_id,
                    task_manager=task_manager,
                    include_uncommitted=include_uncommitted,
                    cwd=repo_path,
                    commit=commit,
                    path_selector=path_selector,
                    offset_bytes=offset_bytes,
                    limit_bytes=limit_bytes,
                    commits_offset=commits_offset,
                    commits_limit=commits_limit,
                    manifest_offset=manifest_offset,
                    manifest_limit=manifest_limit,
                    snapshot_hash=snapshot_hash,
                    view_hash=view_hash,
                    git_timeout_seconds=git_timeout_seconds,
                ),
            )
        except DiffPagingError as exc:
            return exc.as_dict()

    registry.register(
        name="get_task_diff",
        description="Get one byte-oriented page of linked task changes. Follow byte_end and "
        "both cursor_end values; pass snapshot_hash and view_hash on every later page.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "include_uncommitted": {
                    "type": "boolean",
                    "description": "Include uncommitted changes in the diff",
                    "default": False,
                },
                "project_path": {
                    "type": "string",
                    "description": (
                        "Repository path that contains the linked commits. Optional; defaults "
                        "to the task project repository."
                    ),
                    "default": None,
                },
                "commit": {
                    "type": "string",
                    "description": "Optional linked commit SHA selecting one commit view",
                    "default": None,
                },
                "path_selector": {
                    "type": "string",
                    "description": "Opaque selector returned by a manifest item",
                    "default": None,
                },
                "offset_bytes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CURSOR_OFFSET,
                    "default": 0,
                },
                "limit_bytes": {
                    "type": "integer",
                    "minimum": MIN_LIMIT_BYTES,
                    "maximum": MAX_LIMIT_BYTES,
                    "default": MAX_LIMIT_BYTES,
                },
                "commits_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CURSOR_OFFSET,
                    "default": 0,
                },
                "commits_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_COMMITS_LIMIT,
                    "default": MAX_COMMITS_LIMIT,
                },
                "manifest_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_CURSOR_OFFSET,
                    "default": 0,
                },
                "manifest_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_MANIFEST_LIMIT,
                    "default": MAX_MANIFEST_LIMIT,
                },
                "snapshot_hash": {
                    "type": "string",
                    "description": "Snapshot token from the first page",
                    "default": None,
                },
                "view_hash": {
                    "type": "string",
                    "description": "View token from the first page",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=get_task_diff_tool,
    )

    return registry
