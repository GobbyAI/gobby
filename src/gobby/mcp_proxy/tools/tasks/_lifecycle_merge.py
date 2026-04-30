"""Merge lifecycle tool registrations for task MCP tools."""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._transitions import (
    mark_task_merge_failed as transition_mark_task_merge_failed,
)
from gobby.storage.tasks._transitions import (
    mark_task_merged as transition_mark_task_merged,
)
from gobby.storage.tasks._transitions import (
    mark_task_pr_opened as transition_mark_task_pr_opened,
)


def register_mark_task_pr_opened(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the mark_task_pr_opened tool on the given registry."""

    def mark_task_pr_opened(task_id: str, pr_url: str) -> dict[str, Any]:
        """Record the opened PR URL and move a PR-stage task to review."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
            manager_method = getattr(ctx.task_manager, "mark_task_pr_opened", None)
            if callable(manager_method):
                manager_method(resolved_id, pr_url=pr_url)
            else:
                transition_mark_task_pr_opened(ctx.task_manager.db, resolved_id, pr_url)
            return {}
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

    registry.register(
        name="mark_task_pr_opened",
        description="Record an opened PR URL for a PR-stage task and move it to needs_review.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, path, or UUID",
                },
                "pr_url": {
                    "type": "string",
                    "description": "URL of the opened pull request",
                },
            },
            "required": ["task_id", "pr_url"],
        },
        func=mark_task_pr_opened,
    )


def register_mark_task_merged(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the mark_task_merged tool on the given registry."""

    def mark_task_merged(
        task_id: str,
        pr_url: str | None = None,
        merge_sha: str | None = None,
    ) -> dict[str, Any]:
        """Mark a merged task subtree closed and persist merge artifacts."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
            manager_method = getattr(ctx.task_manager, "mark_task_merged", None)
            if callable(manager_method):
                manager_method(resolved_id, pr_url=pr_url, merge_sha=merge_sha)
            else:
                transition_mark_task_merged(
                    ctx.task_manager.db,
                    resolved_id,
                    pr_url=pr_url,
                    merge_sha=merge_sha,
                )
            return {}
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

    registry.register(
        name="mark_task_merged",
        description=(
            "Mark a merge-stage task subtree as merged and closed. Optionally stores "
            "the PR URL and merge commit SHA as task artifacts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, path, or UUID",
                },
                "pr_url": {
                    "type": "string",
                    "description": "Optional pull request URL associated with the merge",
                    "default": None,
                },
                "merge_sha": {
                    "type": "string",
                    "description": "Optional merge commit SHA",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=mark_task_merged,
    )


def register_mark_task_merge_failed(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the mark_task_merge_failed tool on the given registry."""

    def mark_task_merge_failed(
        task_id: str,
        reason: str,
        attended: bool = False,
    ) -> dict[str, Any]:
        """Record a failed merge attempt or attended escalation."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
            manager_method = getattr(ctx.task_manager, "mark_task_merge_failed", None)
            if callable(manager_method):
                if attended:
                    manager_method(resolved_id, reason=reason, attended=True)
                else:
                    manager_method(resolved_id, reason=reason)
            else:
                transition_mark_task_merge_failed(
                    ctx.task_manager.db,
                    resolved_id,
                    reason,
                    attended=attended,
                )
            return {}
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

    registry.register(
        name="mark_task_merge_failed",
        description=(
            "Record a failed merge attempt. In unattended mode the task stays in "
            "merging/open for retry; attended mode escalates it for human resolution."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, path, or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": "Failure reason, such as conflict or verification_failed",
                },
                "attended": {
                    "type": "boolean",
                    "description": "Escalate instead of retrying when human attention is required",
                    "default": False,
                },
            },
            "required": ["task_id", "reason"],
        },
        func=mark_task_merge_failed,
    )
