"""Read-only task-memory review tool registration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.session_resolution import resolve_session_reference
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._id import resolve_task_reference
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.memory.manager import MemoryManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, Task


REVIEW_RECORDS_VARIABLE = "_memory_task_review_records"
_MAX_REVIEW_RECORDS = 50
_CANDIDATE_LIMIT = 5


def _error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": code, "message": message}


def _task_ref(task: Task) -> str:
    return f"#{task.seq_num}" if task.seq_num else task.id


def _closure_id(task: Task) -> str:
    closed_at = task.closed_at.isoformat() if task.closed_at is not None else "open"
    return f"{task.id}:{closed_at}"


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _resolve_session(
    session_manager: SessionManager,
    session_id: str,
) -> tuple[str, Any] | None:
    session = session_manager.get(session_id)
    if session is not None:
        return str(session.id), session

    try:
        resolved_id = resolve_session_reference(session_manager.db, session_id)
    except ValueError:
        return None
    session = session_manager.get(resolved_id)
    return (resolved_id, session) if session is not None else None


def _resolve_task(
    task_manager: LocalTaskManager,
    task_id: str,
    project_id: str,
) -> Task | None:
    try:
        task = task_manager.get_task(task_id)
    except ValueError:
        task = None
    if task is not None:
        return task if task.project_id == project_id else None
    try:
        resolved_id = resolve_task_reference(task_manager.db, task_id, project_id)
    except TaskNotFoundError:
        return None
    try:
        return task_manager.get_task(resolved_id)
    except ValueError:
        return None


def register_memory_review_tools(
    registry: InternalToolRegistry,
    memory_manager: Callable[[], MemoryManager],
    *,
    task_manager: LocalTaskManager | None,
    session_manager: SessionManager | None,
) -> None:
    """Register task-scoped memory review without granting memory writes."""

    @registry.tool(
        name="review_task_memories",
        description=(
            "Search project/global memories related to a task closed by the calling session. "
            "Returns candidates for optional cleanup or durable capture; never writes memories."
        ),
    )
    async def review_task_memories(
        task_id: str,
        changes_summary: str,
        session_id: str,
    ) -> dict[str, Any]:
        summary = changes_summary.strip()
        if not summary:
            return _error(
                "blank_changes_summary",
                "changes_summary must describe the completed work before reviewing memories.",
            )
        if not session_id.strip():
            return _error(
                "missing_session_identity",
                "review_task_memories requires the calling session identity.",
            )
        if task_manager is None or session_manager is None:
            return _error(
                "identity_services_unavailable",
                "Task and session identity services are unavailable.",
            )

        resolved_session = await asyncio.to_thread(_resolve_session, session_manager, session_id)
        if resolved_session is None:
            return _error(
                "missing_session_identity",
                f"Could not resolve calling session {session_id!r}.",
            )
        resolved_session_id, session = resolved_session
        project_id = getattr(session, "project_id", None)
        if not isinstance(project_id, str) or not project_id:
            return _error(
                "missing_project_identity",
                "The calling session has no project identity.",
            )

        task = await asyncio.to_thread(_resolve_task, task_manager, task_id, project_id)
        if task is None:
            return _error(
                "task_not_found",
                f"Could not resolve task {task_id!r} in the calling session's project.",
            )
        if task.closed_at is None:
            return _error("task_not_closed", f"Task {_task_ref(task)} is not closed.")
        if task.closed_in_session_id != resolved_session_id:
            return _error(
                "foreign_session_closure",
                f"Task {_task_ref(task)} was not closed by the calling session.",
            )

        query = f"{task.title}\n\n{summary}"
        try:
            candidates = await memory_manager().search_memories(
                query=query,
                project_id=project_id,
                limit=_CANDIDATE_LIMIT,
                session_id=resolved_session_id,
                caller="mcp_proxy.memory.review_task_memories",
                include_global=True,
            )
        except Exception as exc:
            return _error(
                "memory_search_failed",
                f"Memory search failed for task {_task_ref(task)}: {exc}",
            )

        serialized = [
            {
                "id": candidate.id,
                "content": candidate.content,
                "rationale": getattr(candidate, "rationale", None),
                "type": _enum_value(candidate.memory_type),
                "tags": candidate.tags,
                "similarity": getattr(candidate, "similarity", None),
            }
            for candidate in candidates
        ]
        record = {
            "closure_id": _closure_id(task),
            "task_id": task.id,
            "task_ref": _task_ref(task),
            "candidate_ids": [candidate["id"] for candidate in serialized],
            "reviewed_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(
            SessionVariableManager(session_manager.db).upsert_bounded_list_variable,
            resolved_session_id,
            REVIEW_RECORDS_VARIABLE,
            record,
            identity={"closure_id": record["closure_id"]},
            max_items=_MAX_REVIEW_RECORDS,
        )
        return {
            "success": True,
            "task_id": task.id,
            "task_ref": _task_ref(task),
            "source_task_id": task.id,
            "candidate_count": len(serialized),
            "candidates": serialized,
        }
