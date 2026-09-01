"""One-shot handoff retrieval, feedback capture, and manual session titles."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.sessions.handoff import (
    FEEDBACK_DISPOSITIONS,
    FEEDBACK_FREQUENCIES,
    FEEDBACK_KINDS,
    consume_pending_handoff,
    normalize_feedback_observations,
    write_feedback_batch,
)
from gobby.storage.sessions._title_defaults import MANUAL_TITLE_SOURCE
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, Task


FEEDBACK_OBSERVATION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "kind": {
            "type": "string",
            "enum": list(FEEDBACK_KINDS),
            "description": (
                "Pick the closest listed kind. Use 'other' only when no listed kind fits; "
                "it requires kind_other_label."
            ),
        },
        "kind_other_label": {
            "type": "string",
            "description": (
                "Short label naming the unlisted kind. Required iff kind is 'other'; "
                "rejected when it restates a listed kind. Recurring labels are promoted "
                "to the enum by the nightly review loop."
            ),
        },
        "evidence": {"type": "string"},
        "impact": {"type": "string"},
        "frequency": {"type": "string", "enum": list(FEEDBACK_FREQUENCIES)},
        "suggestion": {"type": "string"},
        "disposition": {
            "type": "string",
            "enum": list(FEEDBACK_DISPOSITIONS),
            "description": (
                "How the observation was handled. An actionable Gobby defect is found work. "
                "'fixed': include the #N task this session claimed and closed, or still has "
                "claimed in progress. 'escalated': include the active owner session ref after "
                "send_message. 'filed-task' is rung 3 only: include the #N task this session "
                "created with needs-decision or clean-window and a description explaining why "
                "rungs 1 and 2 do not apply. Unlabeled or unclaimed filings and every other "
                "defect disposition are shirked found work; the stop gate blocks and the nightly "
                "digest flags them."
            ),
        },
    },
    "required": ["source", "kind", "evidence", "impact", "frequency"],
    "additionalProperties": False,
}


def build_feedback_task_resolver(
    session_manager: SessionManager,
    task_manager: LocalTaskManager | None,
    session_id: str,
) -> Callable[[str], Task | None] | None:
    """Build a project-scoped #N resolver for feedback disposition validation."""
    if task_manager is None:
        return None
    session = session_manager.get(session_id)
    project_id = getattr(session, "project_id", None)
    if not isinstance(project_id, str) or not project_id:
        return None

    from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
    from gobby.storage.tasks import TaskNotFoundError

    def resolve_task(task_ref: str) -> Task | None:
        try:
            task_id = resolve_task_id_for_mcp(task_manager, task_ref, project_id)
            return task_manager.get_task(task_id)
        except (TaskNotFoundError, ValueError):
            return None

    return resolve_task


def register_handoff_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    task_manager: LocalTaskManager | None = None,
) -> None:
    """Register pull-only handoff, feedback, and title tools."""

    def _current_session_id() -> str | None:
        current = get_current_session_id()
        if not current:
            return None
        try:
            return session_manager.resolve_session_reference(current)
        except ValueError:
            return None

    def get_handoff() -> dict[str, Any]:
        """Consume the handoff staged for this compact or clear continuation."""
        session_id = _current_session_id()
        if session_id is None:
            return {"success": False, "error": "No session context available"}
        consumed = consume_pending_handoff(session_manager.db, session_id)
        if consumed is None:
            return {
                "success": True,
                "found": False,
                "handoff": "",
                "required_skills": [],
                "advisory_skills": [],
            }
        return {
            "success": True,
            "found": True,
            "session_id": consumed.session_id,
            "handoff": consumed.markdown,
            "required_skills": list(consumed.required_skills),
            "advisory_skills": list(consumed.advisory_skills),
        }

    def feedback(observations: list[dict[str, Any]]) -> dict[str, Any]:
        """Store structured observations about Gobby behavior for later review."""
        session_id = _current_session_id()
        if session_id is None:
            return {"success": False, "error": "No session context available"}
        try:
            normalized = normalize_feedback_observations(
                observations,
                resolve_task=build_feedback_task_resolver(
                    session_manager,
                    task_manager,
                    session_id,
                ),
                session_id=session_id,
            )
            ids = write_feedback_batch(session_manager.db, session_id, normalized)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "error_code": "invalid_feedback"}
        return {"success": True, "created": len(ids), "feedback_ids": ids}

    def set_title(title: str) -> dict[str, Any]:
        """Set a sticky manual title for the current session."""
        session_id = _current_session_id()
        if session_id is None:
            return {"success": False, "error": "No session context available"}
        if not isinstance(title, str) or not title.strip():
            return {
                "success": False,
                "error": "title must be a nonblank string",
                "error_code": "invalid_title",
            }
        updated = session_manager.update_title(
            session_id,
            title.strip(),
            title_source=MANUAL_TITLE_SOURCE,
        )
        if updated is None:
            return {"success": False, "error": "Session not found"}
        return {"success": True, "session_id": session_id, "title": updated.title}

    registry.register(
        name="get_handoff",
        description=(
            "Consume the one pending handoff created by set_handoff for this compact "
            "continuation or direct clear predecessor. Returns an empty result for manual "
            "provider compact/clear operations and on subsequent calls."
        ),
        brief="Consume the pending structured handoff for this continuation.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        func=get_handoff,
    )
    registry.register(
        name="feedback",
        description="Store zero or more structured Gobby feedback observations atomically.",
        brief="Store structured feedback observations for later review.",
        input_schema={
            "type": "object",
            "properties": {
                "observations": {
                    "type": "array",
                    "items": FEEDBACK_OBSERVATION_INPUT_SCHEMA,
                }
            },
            "required": ["observations"],
            "additionalProperties": False,
        },
        func=feedback,
    )
    registry.register(
        name="set_title",
        description="Set a sticky manual title for the current session.",
        brief="Set the current session's sticky manual title.",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
        func=set_title,
    )
