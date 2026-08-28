"""Grok in-place compact closeout: treat PostCompact as compact context loss."""

from __future__ import annotations

from typing import Any

from gobby.hooks.event_handlers._session_start.handoff import (
    prepare_compact_continuation_variables,
)


def apply_in_place_compact_context_loss(handler: Any, session_id: str | None) -> None:
    """Refresh compact-epoch tracking on the live row after Grok PostCompact.

    Grok never emits SessionStart(source=compact). This is the same-row
    equivalent of compact SessionStart tracking resets plus handoff prep.
    """
    prepare_compact_continuation_variables(handler, session_id, "compact")
    if not session_id or handler._session_manager is None:
        return

    from gobby.hooks.event_handlers._session_start.materialize import (
        _reset_agent_context_injection,
    )
    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)
    _reset_agent_context_injection(handler, session_id)

    updates: dict[str, Any] = {
        "unlocked_tools": [],
        "suggested_skill_names": [],
        "loaded_skills": [],
        "workflow_requested_skills": [],
        "injected_memory_ids": [],
    }
    sv_mgr.merge_variables(session_id, updates)

    session = handler._session_manager.get(session_id)
    project_id = getattr(session, "project_id", None) if session is not None else None
    if project_id:
        from gobby.hooks.event_handlers._session_responses import (
            build_claimed_task_context,
        )

        claimed = build_claimed_task_context(handler, session_id, project_id, compact=False)
        if claimed:
            sv_mgr.merge_variables(session_id, {"task_context": claimed})
