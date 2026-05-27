"""GitHub integration MCP tools for task management.

Provides tools for importing GitHub issues as tasks and linking tasks to issues:
- import_github_issues: Import issues from a GitHub repo as gobby tasks
- link_task_to_github_issue: Link an existing task to a GitHub issue

Extracted as a separate registry following the Strangler Fig pattern.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.storage.tasks import LocalTaskManager

__all__ = ["create_github_registry"]

logger = logging.getLogger(__name__)


def _fetch_issues_via_gh(
    repo: str,
    labels: list[str] | None = None,
    state: str = "open",
) -> list[dict[str, Any]]:
    """Fetch GitHub issues using the gh CLI.

    Args:
        repo: GitHub repo in "owner/repo" format.
        labels: Optional label filter.
        state: Issue state filter.

    Returns:
        List of issue dicts with number, title, body, labels keys.

    Raises:
        RuntimeError: If gh CLI fails.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--json",
        "number,title,body,labels",
        "--limit",
        "100",
    ]
    if labels:
        cmd.extend(["--label", ",".join(labels)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {result.stderr.strip()}")

    return list(json.loads(result.stdout))


def create_github_registry(
    ctx: RegistryContext,
) -> InternalToolRegistry:
    """Create a registry with GitHub integration tools.

    Args:
        ctx: RegistryContext providing task manager and shared services.

    Returns:
        InternalToolRegistry with GitHub tools registered.
    """
    from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

    task_manager = ctx.task_manager

    registry = InternalToolRegistry(
        name="gobby-tasks-github",
        description="GitHub integration tools for tasks",
    )

    # --- import_github_issues ---

    def import_github_issues(
        repo: str,
        labels: list[str] | None = None,
        state: str = "open",
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Import GitHub issues as gobby tasks with deduplication."""
        from gobby.utils.session_context import get_current_session_id

        logger.info("Importing GitHub issues from %s (session=%s)", repo, get_current_session_id())
        # Validate repo format
        if "/" not in repo or repo.count("/") != 1:
            return {
                "success": False,
                "error": f"Invalid repo format '{repo}'. Expected 'owner/repo'.",
            }

        context = get_project_context()
        project_id = context.get("id") if context else None
        if not project_id:
            return {"success": False, "error": "No project context available"}

        # Fetch issues via gh CLI
        try:
            issues = _fetch_issues_via_gh(repo=repo, labels=labels, state=state)
        except FileNotFoundError:
            return {"success": False, "error": "gh CLI not found. Install GitHub CLI."}
        except Exception as e:
            return {"success": False, "error": str(e)}

        imported = []
        updated = []

        for issue in issues:
            issue_number = issue.get("number")
            title = issue.get("title", "Untitled Issue")
            body = issue.get("body", "")
            issue_labels = [
                lbl["name"] if isinstance(lbl, dict) else lbl for lbl in (issue.get("labels") or [])
            ]

            # Dedup: check existing task by repo + issue number
            existing = _find_task_by_github_issue(task_manager, repo, issue_number, project_id)
            if existing:
                task_manager.update_task(
                    existing.id,
                    title=title,
                    description=body,
                    labels=issue_labels or None,
                )
                updated.append(task_manager.get_task(existing.id).to_brief())
            else:
                task = task_manager.create_task(
                    project_id=project_id,
                    title=title,
                    description=body,
                    github_issue_number=issue_number,
                    github_repo=repo,
                    labels=issue_labels or None,
                )
                # Set parent if specified
                if parent_task_id:
                    try:
                        resolved_parent = resolve_task_id_for_mcp(
                            task_manager, parent_task_id, project_id
                        )
                        task_manager.update_task(task.id, parent_task_id=resolved_parent)
                    except Exception as e:
                        logger.warning(f"Failed to set parent task: {e}")
                imported.append(task.to_brief())

        return {
            "success": True,
            "imported_count": len(imported),
            "updated_count": len(updated),
            "tasks": imported + updated,
        }

    registry.register(
        name="import_github_issues",
        description=(
            "Import GitHub issues as gobby tasks. Deduplicates on re-import "
            "(updates existing tasks instead of creating duplicates). "
            "Uses gh CLI — requires GitHub CLI installed and authenticated."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": 'GitHub repo in "owner/repo" format',
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter issues by labels (optional)",
                    "default": None,
                },
                "state": {
                    "type": "string",
                    "description": 'Issue state: "open", "closed", or "all"',
                    "default": "open",
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Optional parent task to nest imported issues under",
                    "default": None,
                },
            },
            "required": ["repo"],
        },
        func=import_github_issues,
    )

    # --- link_task_to_github_issue ---

    def link_task_to_github_issue(
        task_id: str,
        repo: str,
        issue_number: int,
    ) -> dict[str, Any]:
        """Link an existing gobby task to a GitHub issue."""
        context = get_project_context()
        project_id = context.get("id") if context else None

        try:
            resolved_id = resolve_task_id_for_mcp(task_manager, task_id, project_id)
        except Exception as e:
            return {"success": False, "error": f"Could not resolve task: {e}"}

        if "/" not in repo or repo.count("/") != 1:
            return {
                "success": False,
                "error": f"Invalid repo format '{repo}'. Expected 'owner/repo'.",
            }

        task_manager.update_task(
            resolved_id,
            github_issue_number=issue_number,
            github_repo=repo,
        )

        task = task_manager.get_task(resolved_id)
        return {
            "success": True,
            "task_id": resolved_id,
            "github_repo": repo,
            "github_issue_number": issue_number,
            "task": task.to_brief(),
        }

    registry.register(
        name="link_task_to_github_issue",
        description=(
            "Link an existing gobby task to a GitHub issue. "
            "Sets github_issue_number and github_repo on the task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, path, or UUID",
                },
                "repo": {
                    "type": "string",
                    "description": 'GitHub repo in "owner/repo" format',
                },
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue number",
                },
            },
            "required": ["task_id", "repo", "issue_number"],
        },
        func=link_task_to_github_issue,
    )

    return registry


def _find_task_by_github_issue(
    task_manager: LocalTaskManager,
    repo: str,
    issue_number: int | None,
    project_id: str,
) -> Any | None:
    """Find an existing task linked to a GitHub issue."""
    if issue_number is None:
        return None
    row = task_manager.db.execute(
        "SELECT id FROM tasks WHERE github_repo = %s AND github_issue_number = %s "
        "AND project_id = %s LIMIT 1",
        (repo, issue_number, project_id),
    ).fetchone()
    if row:
        return task_manager.get_task(row["id"])
    return None
