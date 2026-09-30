"""Guarded correction of a closed task's originally reviewed commit marker."""

import asyncio
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_repo_paths import resolve_task_repo_path
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import _canonical_commit_sha
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import TaskNotFoundError
from gobby.utils.session_context import get_current_session_id


def register_closed_candidate_repair(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def repair_closed_candidate(
        task_id: str,
        review_id: str,
        expected_closed_commit_sha: str,
        reason: str,
        project_path: str | None = None,
        preview: bool = True,
    ) -> dict[str, Any]:
        try:
            resolved_id = await asyncio.to_thread(
                resolve_task_id_for_mcp, ctx.task_manager, task_id
            )
            task = await asyncio.to_thread(ctx.task_manager.get_task, resolved_id)
            review = await asyncio.to_thread(
                TaskCloseReviewStore(ctx.task_manager.db).get, review_id
            )
            caller = get_current_session_id()
            if task is None or review is None or caller is None:
                raise ValueError("A task, original close review and active caller are required.")
            caller_id = await asyncio.to_thread(ctx.resolve_session_id, caller)
            session = await asyncio.to_thread(ctx.session_manager.get, caller_id)
            if session is None or not session.machine_id:
                raise ValueError("Repair requires a registered caller with a machine identity.")
            repo_path = await asyncio.to_thread(
                resolve_task_repo_path,
                task_manager=ctx.task_manager,
                project_manager=ctx.project_manager,
                task=task,
                project_path=project_path,
                machine_id=session.machine_id,
            )
            requested = review.close_arguments.get("commit_sha")
            if repo_path is None or not isinstance(requested, str):
                raise ValueError(
                    "The original explicit candidate and registered checkout are required."
                )
            candidate = await _canonical_commit_sha(requested, cwd=repo_path)
            if candidate is None:
                raise ValueError("Git cannot resolve the originally reviewed commit object.")
            result = await asyncio.to_thread(
                ctx.task_manager.repair_closed_candidate,
                resolved_id,
                review_id=review_id,
                candidate_commit_sha=candidate,
                expected_closed_commit_sha=expected_closed_commit_sha,
                by_session_id=caller_id,
                reason=reason,
                preview=preview,
            )
            return {"success": True, "preview": preview, **result}
        except (TaskNotFoundError, ValueError) as exc:
            return {
                "success": False,
                "error": "closed_candidate_repair_refused",
                "message": str(exc),
            }

    registry.register(
        name="repair_closed_candidate",
        description=(
            "Correct a CLOSED/VALID task's commit marker to the exact full candidate from its "
            "original completed VALID close review. Defaults to read-only preview. Apply preserves "
            "closure, review evidence and all links, and records a transactional lifecycle audit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "review_id": {"type": "string"},
                "expected_closed_commit_sha": {"type": "string"},
                "reason": {"type": "string", "minLength": 20},
                "project_path": {"type": "string"},
                "preview": {"type": "boolean", "default": True},
            },
            "required": ["task_id", "review_id", "expected_closed_commit_sha", "reason"],
            "additionalProperties": False,
        },
        func=repair_closed_candidate,
    )
