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
from gobby.workflows.memory_review_conditions import pending_memory_reviews_complete
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.memory.manager import MemoryManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, Task


REVIEW_RECORDS_VARIABLE = "_memory_task_review_records"
REVIEW_DELIVERED_VARIABLE = "_memory_review_stop_delivered"
_MAX_REVIEW_RECORDS = 50
_CANDIDATE_LIMIT = 5


def _error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": code, "message": message}


def _task_ref(task: Task) -> str:
    return f"#{task.seq_num}" if task.seq_num else task.id


def _closure_id(task: Task) -> str:
    closed_at = task.closed_at.isoformat() if task.closed_at is not None else "open"
    return f"{task.id}:{closed_at}"


def _record_review(state: SessionVariableManager, session_id: str, record: dict[str, Any]) -> bool:
    """Persist one review record; return whether the queued batch is now fully reviewed.

    A fully reviewed batch releases the post-close stop/compact gate exactly as
    delivering its block would, so proactive reviews are never re-requested.
    """
    state.upsert_bounded_list_variable(
        session_id,
        REVIEW_RECORDS_VARIABLE,
        record,
        identity={"closure_id": record["closure_id"]},
        max_items=_MAX_REVIEW_RECORDS,
    )
    if not pending_memory_reviews_complete(state.get_variables(session_id)):
        return False
    state.set_variable(session_id, REVIEW_DELIVERED_VARIABLE, True)
    return True


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
            "Returns candidates for optional cleanup or durable capture; never writes memories. "
            "Reviewing every queued closure releases the post-close stop/compact review gate."
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
                # Embed the summary verbatim instead of letting the search
                # service run YAKE over it (#21402). YAKE strips conversational
                # noise from short prompts by keeping the terms a text repeats,
                # which inverts on a changes_summary: the repeated terms are
                # boilerplate ("src", "gobby", "config", "tests", "Added") and
                # the rare ones are the identifiers a memory records. Measured
                # on #21394's 461-word summary, YAKE's ten keywords dropped
                # `adapter_timeout`, `workflow.timeout`, and
                # `validate_hook_timeout_order`, and raising its cap to fifty
                # still never reached them. Embedding verbatim scored the two
                # memories that change invalidated at 0.8436/0.7058 against
                # 0.7192/0.5976 for the YAKE query, and widened the margin over
                # an unrelated memory rather than trading recall for noise.
                # Length is not the risk dilution arguments assume: the same
                # target holds 0.8423 -> 0.8321 from 887 to 6991 tokens.
                embed_text=query,
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
        reviews_complete = await asyncio.to_thread(
            _record_review, SessionVariableManager(session_manager.db), resolved_session_id, record
        )
        return {
            "success": True,
            "task_id": task.id,
            "task_ref": _task_ref(task),
            "source_task_id": task.id,
            "candidate_count": len(serialized),
            "candidates": serialized,
            "pending_reviews_complete": reviews_complete,
        }
