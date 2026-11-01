"""MCP registration for synchronous task closure."""

from __future__ import annotations

from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _evaluate_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import commit_close


def register_close_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def close_task(
        task_id: str,
        reason: str = "done",
        changes_summary: str | None = None,
        commit_sha: str | None = None,
        project_path: str | None = None,
        preview: bool = False,
        response_detail: Literal["concise", "diagnostic"] = "concise",
        superseded_by: str | None = None,
        expected_closure_revision: str | None = None,
    ) -> dict[str, Any]:
        evaluation = await _evaluate_close(
            ctx,
            task_id=task_id,
            reason=reason,
            commit_sha=commit_sha,
            project_path=project_path,
            response_detail=response_detail,
            superseded_by=superseded_by,
        )
        if expected_closure_revision is not None and (
            evaluation.decision is None
            or evaluation.decision.snapshot_revision != expected_closure_revision
        ):
            return evaluation.refuse(
                "close_state_changed",
                "Task, evidence or policy changed since preview; retry close_task.",
            ).response(preview=preview)
        if preview or not evaluation.ready or evaluation.already_closed:
            return evaluation.response(preview=preview)
        return await commit_close(
            ctx,
            evaluation,
            reason=reason,
            changes_summary=changes_summary or "",
            commit_sha=commit_sha,
            superseded_by=superseded_by,
        )

    registry.register(
        name="close_task",
        description=(
            "Close a task when its active definition's selected task_close rules allow it. "
            "Rules evaluate authenticated attestations and task facts. commit_sha optionally "
            "links a commit atomically with closure. preview=true evaluates the same policy "
            "without changing task state and returns policy and snapshot revisions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task reference (#N, path, or UUID)."},
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                    "default": "done",
                },
                "changes_summary": {"type": "string"},
                "superseded_by": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional related task ref or commit SHA, exposed to closure policy."
                    ),
                },
                "commit_sha": {"type": "string", "minLength": 1},
                "project_path": {"type": "string"},
                "preview": {"type": "boolean", "default": False},
                "expected_closure_revision": {
                    "type": "string",
                    "description": "Require this snapshot_revision from an earlier preview.",
                },
                "response_detail": {
                    "type": "string",
                    "enum": ["concise", "diagnostic"],
                    "default": "concise",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        func=close_task,
    )
