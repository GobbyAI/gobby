"""Delivery-state MCP tools for PR and merge orchestration."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.delivery import TaskDeliveryStateManager


def _resolve_task(ctx: RegistryContext, task_id: str) -> str:
    return resolve_task_id_for_mcp(ctx.task_manager, task_id)


def create_delivery_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create delivery-state task ops tools."""
    registry = InternalToolRegistry(
        name="gobby-tasks-delivery",
        description="PR and merge delivery state tools",
    )

    def get_delivery_state(task_id: str) -> dict[str, Any]:
        """Read PR and merge delivery state for a task."""
        resolved_id = _resolve_task(ctx, task_id)
        return {
            "ok": True,
            "task_id": resolved_id,
            "delivery": TaskDeliveryStateManager(ctx.task_manager.db).get_state(resolved_id),
        }

    registry.register(
        name="get_delivery_state",
        description="Read PR and merge delivery state for a task.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        output_schema={"type": "object"},
        func=get_delivery_state,
    )

    def record_pr_state(
        task_id: str,
        unit_key: str | None = None,
        worktree_id: str | None = None,
        repo: str | None = None,
        source_branch: str | None = None,
        target_branch: str | None = None,
        pr_required: bool | None = None,
        protection: dict[str, Any] | None = None,
        pr_url: str | None = None,
        github_pr_number: int | None = None,
        gate_snapshot: dict[str, Any] | None = None,
        pr_state: str | None = None,
        local_update_attempts: int | None = None,
        merge_strategy: str | None = None,
        campaign_state: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        """Record PR delivery state without mutating the task stage."""
        resolved_id = _resolve_task(ctx, task_id)
        manager = TaskDeliveryStateManager(ctx.task_manager.db)
        if campaign_state is not None or merge_strategy is not None:
            manager.record_campaign(
                resolved_id,
                state=campaign_state,
                merge_strategy=merge_strategy,
                last_error=last_error,
            )
        manager.record_unit(
            resolved_id,
            unit_key=unit_key,
            worktree_id=worktree_id,
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            pr_required=pr_required,
            protection_json=protection,
            pr_url=pr_url,
            github_pr_number=github_pr_number,
            gate_snapshot_json=gate_snapshot,
            pr_state=pr_state,
            local_update_attempts=local_update_attempts,
            last_error=last_error,
        )
        return {
            "ok": True,
            "task_id": resolved_id,
            "delivery": manager.get_state(resolved_id),
        }

    registry.register(
        name="record_pr_state",
        description="Record PR delivery state without mutating the task stage.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "unit_key": {"type": ["string", "null"]},
                "worktree_id": {"type": ["string", "null"]},
                "repo": {"type": ["string", "null"]},
                "source_branch": {"type": ["string", "null"]},
                "target_branch": {"type": ["string", "null"]},
                "pr_required": {"type": ["boolean", "null"]},
                "protection": {"type": ["object", "null"]},
                "pr_url": {"type": ["string", "null"]},
                "github_pr_number": {"type": ["integer", "null"]},
                "gate_snapshot": {"type": ["object", "null"]},
                "pr_state": {"type": ["string", "null"]},
                "local_update_attempts": {"type": ["integer", "null"]},
                "merge_strategy": {"type": ["string", "null"]},
                "campaign_state": {"type": ["string", "null"]},
                "last_error": {"type": ["string", "null"]},
            },
            "required": ["task_id"],
        },
        output_schema={"type": "object"},
        func=record_pr_state,
    )

    return registry
