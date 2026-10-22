"""Role-owned task step record tools for the gobby-tasks registry.

Each tool is a thin transport over ``gobby.tasks.step_records.record_task_step``.
The step is fixed per tool. The storage writer enforces the writer grant, project
membership, epoch and round rules, commit linkage, reviewer independence and replay.
This module adds only session and task resolution, full commit SHA resolution, and
the response shape.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_commits import session_machine_id
from gobby.mcp_proxy.tools.task_repo_paths import RepoPathValidationError, resolve_task_repo_path
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.lifecycle_access import CrossProjectTaskForbidden, resolve_lifecycle_task
from gobby.tasks.step_records import StepRecordError, StepSubmission, TaskStep, record_task_step
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.session_context import get_current_session_id

_TASK_ID_DESCRIPTION = (
    "Task reference: #N, N, path, <project>#N, or UUID; foreign tasks require coordinator authority"
)

_STEP_TOOLS: tuple[tuple[str, TaskStep, str], ...] = (
    (
        "record_test_step",
        "test",
        "Record a test-step result for a task. Only a session whose installed agent "
        "definition grants the test step may write it. An exact replay returns the stored "
        "record with created=false.",
    ),
    (
        "record_implementation_step",
        "implementation",
        "Record an implementation-step result for a task at its latest linked commit. Only "
        "a session whose installed agent definition grants the implementation step may "
        "write it.",
    ),
    (
        "record_review_step",
        "review",
        "Record one review round for a task. Report infrastructure errors (tool crash, "
        "sandbox loss) as pending or in_progress with feedback; those never count. "
        "rejected is a judged rejection. The third rejection in an epoch escalates the "
        "task; only an audited reset (de-escalation) starts a new epoch.",
    ),
    (
        "record_closing_step",
        "closing",
        "Record the closing-step result for a task at its latest linked commit. Only a "
        "session whose installed agent definition grants the closing step may write it.",
    ),
)


def _input_schema() -> dict[str, Any]:
    """Closed schema: the step, writer session, agent identity and version are never inputs."""
    return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": _TASK_ID_DESCRIPTION},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "approved", "rejected"],
                "description": "Result status for this round",
            },
            "epoch": {
                "type": "integer",
                "minimum": 1,
                "description": "Review epoch the record belongs to",
            },
            "round_number": {
                "type": "integer",
                "minimum": 1,
                "description": "Review round within the epoch",
            },
            "feedback": {
                "type": "string",
                "minLength": 1,
                "description": "Evidence for the result, stored in full",
            },
            "commit_sha": {
                "type": "string",
                "description": "Commit ref, resolved to its full 40-character SHA",
            },
            "disposition": {
                "type": "string",
                "enum": ["moot", "superseded", "wont_do"],
                "description": "No-work disposition, when there is no commit evidence",
            },
        },
        "required": ["task_id", "status", "epoch", "round_number", "feedback"],
        "additionalProperties": False,
    }


async def resolve_full_commit_sha(sha: str, repo_path: str) -> str | None:
    """Resolve a commit ref to its full SHA; unknown, ambiguous or non-commit refs give None."""
    result = await daemon_git.run(
        ("rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}"),
        cwd=repo_path,
    )
    if not isinstance(result, GitOk):
        return None
    return result.stdout.strip() or None


def _validation_message(error: ValidationError) -> str:
    return "; ".join(str(item["msg"]) for item in error.errors())


async def _record_step(
    ctx: RegistryContext,
    step: TaskStep,
    *,
    task_id: str,
    status: str,
    epoch: int,
    round_number: int,
    feedback: str,
    commit_sha: str | None,
    disposition: str | None,
) -> dict[str, Any]:
    author_session_id = get_current_session_id()
    if not author_session_id:
        return {"error": "No session context available; a step record needs a calling session."}
    caller = ctx.session_manager.get(author_session_id)
    project_id = caller.project_id if caller is not None else ctx.get_current_project_id()
    if project_id is None:
        return {"error": "No project context available."}
    try:
        task = await asyncio.to_thread(
            resolve_lifecycle_task,
            ctx.task_manager,
            task_id,
            project_id=project_id,
            caller_session_id=author_session_id,
            sessions=ctx.session_manager,
        )
    except CrossProjectTaskForbidden as e:
        return {"error": str(e)}
    except (TaskNotFoundError, ValueError) as e:
        return {"error": f"Invalid task_id: {e}"}

    full_sha: str | None = None
    if commit_sha is not None:
        try:
            repo_path = resolve_task_repo_path(
                task_manager=ctx.task_manager,
                project_manager=ctx.project_manager,
                task=task,
                project_path=None,
                machine_id=session_machine_id(ctx.session_manager, task.project_id),
            )
        except (RepoPathValidationError, ValueError) as e:
            return {"error": str(e)}
        if repo_path is None:
            return {"error": f"No repository checkout is available to resolve {commit_sha}."}
        full_sha = await resolve_full_commit_sha(commit_sha, repo_path)
        if full_sha is None:
            return {"error": f"Invalid or unresolved commit SHA: {commit_sha}"}

    try:
        submission = StepSubmission.model_validate(
            {
                "status": status,
                "epoch": epoch,
                "round_number": round_number,
                "feedback": feedback,
                "commit_sha": full_sha,
                "disposition": disposition,
            }
        )
    except ValidationError as e:
        return {"error": _validation_message(e)}

    try:
        record, created = await asyncio.to_thread(
            record_task_step,
            ctx.task_manager.db,
            task_id=task.id,
            session_id=author_session_id,
            step=step,
            submission=submission,
        )
    except StepRecordError as e:
        return {"error": str(e)}
    if created:
        # The storage transaction has committed; listeners observe the new record.
        ctx.task_manager._notify_listeners()
    return {"record": record.model_dump(mode="json"), "created": created}


def _step_tool(ctx: RegistryContext, step: TaskStep) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Bind one dedicated writer. Explicit parameters only, so unknown keys are refused."""

    async def record(
        task_id: str,
        status: str,
        epoch: int,
        round_number: int,
        feedback: str,
        commit_sha: str | None = None,
        disposition: str | None = None,
    ) -> dict[str, Any]:
        return await _record_step(
            ctx,
            step,
            task_id=task_id,
            status=status,
            epoch=epoch,
            round_number=round_number,
            feedback=feedback,
            commit_sha=commit_sha,
            disposition=disposition,
        )

    return record


def create_step_record_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create the four role-owned task step record tools."""
    registry = InternalToolRegistry(
        name="gobby-tasks-steps",
        description="Role-owned task step records",
    )
    for name, step, description in _STEP_TOOLS:
        registry.register(
            name=name,
            description=description,
            input_schema=_input_schema(),
            func=_step_tool(ctx, step),
        )
    return registry
