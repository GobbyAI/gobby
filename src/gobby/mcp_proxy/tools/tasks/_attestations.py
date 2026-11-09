"""Thin generic task-evidence transport; policy belongs to selected rules."""

import asyncio
from dataclasses import asdict
from typing import Any

from pydantic import ValidationError

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.tasks import Task, TaskNotFoundError
from gobby.tasks.attestations import (
    AttestationError,
    AttestationSubmission,
    list_attestations,
    record_attestation_recording,
)
from gobby.tasks.lifecycle_access import resolve_lifecycle_task
from gobby.utils.session_context import get_current_session_id


def create_attestation_registry(ctx: RegistryContext) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="gobby-task-attestations", description="Task evidence")

    def resolve(task_id: str) -> tuple[Task, str]:
        caller_id = get_current_session_id()
        caller = ctx.session_manager.get(caller_id) if caller_id else None
        if caller is None or caller.project_id is None:
            raise AttestationError("task evidence requires an authenticated project session")
        task = resolve_lifecycle_task(
            ctx.task_manager,
            task_id,
            project_id=caller.project_id,
            caller_session_id=caller.id,
            sessions=ctx.session_manager,
        )
        if task.project_id != caller.project_id:
            raise AttestationError("task evidence access requires a session in the task project")
        return task, caller.id

    async def record(task_id: str, **kwargs: Any) -> dict[str, Any]:
        try:
            task, author = await asyncio.to_thread(resolve, task_id)
            submission = AttestationSubmission.model_validate(kwargs)
            recording = await asyncio.to_thread(
                record_attestation_recording,
                ctx.task_manager.db,
                task_id=task.id,
                session_id=author,
                submission=submission,
            )
        except (ValueError, ValidationError, TaskNotFoundError) as exc:
            return {"error": str(exc)}
        if recording.created:
            ctx.task_manager._notify_listeners()
        result: dict[str, Any] = {
            "record": recording.record.model_dump(mode="json"),
            "created": recording.created,
        }
        if recording.claim_flow_stalled is not None:
            result["claim_flow_stalled"] = asdict(recording.claim_flow_stalled)
        return result

    async def listing(task_id: str) -> dict[str, Any]:
        try:
            task, _ = await asyncio.to_thread(resolve, task_id)
            records = await asyncio.to_thread(list_attestations, ctx.task_manager.db, task.id)
        except (ValueError, TaskNotFoundError) as exc:
            return {"error": str(exc)}
        return {"attestations": [record.model_dump(mode="json") for record in records]}

    schema = AttestationSubmission.model_json_schema()
    schema["properties"]["task_id"] = {"type": "string", "minLength": 1}
    schema["required"].append("task_id")
    registry.register(
        name="record_attestation",
        description="Append authenticated task evidence.",
        input_schema=schema,
        func=record,
    )
    registry.register(
        name="list_attestations",
        description="List immutable task evidence history.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        func=listing,
    )
    return registry
