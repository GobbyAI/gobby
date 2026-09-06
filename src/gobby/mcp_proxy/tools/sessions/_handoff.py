"""One-shot handoff retrieval, feedback capture, and manual session titles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.sessions.clear_continuation import resolve_clear_successor
from gobby.sessions.handoff import (
    FEEDBACK_DISPOSITIONS,
    FEEDBACK_FREQUENCIES,
    FEEDBACK_KINDS,
    FEEDBACK_SOURCE_SURFACES,
    consume_pending_handoff,
    normalize_feedback_observations,
    write_feedback_batch,
)
from gobby.sessions.handoff_records import agent_run_attempt_id, get_agent_end_handoff
from gobby.storage.sessions._title_defaults import MANUAL_TITLE_SOURCE
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, Task


@dataclass(frozen=True, slots=True)
class _FeedbackTaskResolver:
    resolve_task: Callable[[str], Task | None]
    descendant_session_ids: frozenset[str]


FEEDBACK_OBSERVATION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": (
                "Name a Gobby surface as gobby-<server>:<tool>, <surface>:<name> where "
                f"surface is one of {', '.join(FEEDBACK_SOURCE_SURFACES)}, or a repository "
                "path starting with src/gobby/, crates/, web/src/, or docs/."
            ),
        },
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
                "'fixed': include the #N task claimed or closed by this session or by a "
                "spawned descendant session. 'escalated': include the active owner session "
                "ref after "
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
) -> _FeedbackTaskResolver | None:
    """Build a project-scoped #N resolver for feedback disposition validation."""
    if task_manager is None:
        return None
    session = session_manager.get(session_id)
    project_id = getattr(session, "project_id", None)
    if not isinstance(project_id, str) or not project_id:
        return None

    from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
    from gobby.storage.tasks import TaskNotFoundError

    descendant_rows = session_manager.db.fetchall(
        """
        WITH RECURSIVE descendant_sessions(session_id, depth) AS (
            SELECT child_session_id, 1
            FROM agent_runs
            WHERE parent_session_id = %s
              AND child_session_id IS NOT NULL
            UNION
            SELECT runs.child_session_id, descendants.depth + 1
            FROM agent_runs AS runs
            JOIN descendant_sessions AS descendants
              ON runs.parent_session_id = descendants.session_id
            WHERE descendants.depth < 5
              AND runs.child_session_id IS NOT NULL
        )
        SELECT DISTINCT session_id
        FROM descendant_sessions
        """,
        (session_id,),
    )
    descendant_session_ids = frozenset(str(row["session_id"]) for row in descendant_rows)

    def resolve_task(task_ref: str) -> Task | None:
        try:
            task_id = resolve_task_id_for_mcp(task_manager, task_ref, project_id)
            return task_manager.get_task(task_id)
        except (TaskNotFoundError, ValueError):
            return None

    return _FeedbackTaskResolver(resolve_task, descendant_session_ids)


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

    async def get_handoff(agent_run_id: str | None = None) -> dict[str, Any]:
        """Consume a continuation handoff or read one agent run's final handoff."""
        session_id = _current_session_id()
        if session_id is None:
            return {"success": False, "error": "No session context available"}
        if agent_run_id is not None:
            try:
                agent_run_attempt_id(agent_run_id)
            except ValueError:
                return {
                    "success": False,
                    "error": f"Agent run {agent_run_id} not found",
                    "error_code": "run_not_found",
                }
            run = session_manager.db.fetchone(
                "SELECT id, parent_session_id FROM agent_runs WHERE id = %s",
                (agent_run_id,),
            )
            if run is None:
                return {
                    "success": False,
                    "error": f"Agent run {agent_run_id} not found",
                    "error_code": "run_not_found",
                }
            parent_session_id = str(run["parent_session_id"])
            if session_id != parent_session_id:
                successor = resolve_clear_successor(session_manager.db, parent_session_id)
                if successor != session_id:
                    return {
                        "success": False,
                        "error": "Agent handoff is available only to the parent session",
                        "error_code": "access_denied",
                    }
            delivered = get_agent_end_handoff(session_manager.db, agent_run_id)
            if delivered is None:
                return {
                    "success": True,
                    "found": False,
                    "handoff_id": None,
                    "agent_run_id": agent_run_id,
                    "session_id": None,
                    "boundary_kind": "agent_end",
                    "handoff": "",
                }
            return {
                "success": True,
                "found": True,
                "handoff_id": delivered.id,
                "agent_run_id": agent_run_id,
                "session_id": delivered.session_id,
                "boundary_kind": "agent_end",
                "handoff": delivered.payload.rendered_markdown,
            }
        consumed = consume_pending_handoff(session_manager.db, session_id)
        if consumed is None:
            return {
                "success": True,
                "found": False,
                "session_id": None,
                "handoff": "",
            }
        return {
            "success": True,
            "found": True,
            "session_id": consumed.session_id,
            "handoff": consumed.markdown,
        }

    def feedback(observations: list[dict[str, Any]]) -> dict[str, Any]:
        """Store structured observations about Gobby behavior for later review."""
        session_id = _current_session_id()
        if session_id is None:
            return {"success": False, "error": "No session context available"}
        try:
            task_resolver = build_feedback_task_resolver(
                session_manager,
                task_manager,
                session_id,
            )
            normalized = normalize_feedback_observations(
                observations,
                resolve_task=(task_resolver.resolve_task if task_resolver is not None else None),
                descendant_session_ids=(
                    task_resolver.descendant_session_ids if task_resolver is not None else ()
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
            "With no arguments, consume the one pending compact/clear handoff. With "
            "agent_run_id, idempotently read that child run's final agent_end handoff as "
            "its parent session or a bound clear successor."
        ),
        brief="Consume a continuation handoff or read a child run's final handoff.",
        input_schema={
            "type": "object",
            "properties": {"agent_run_id": {"type": "string"}},
            "additionalProperties": False,
        },
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
