"""Built-in observer dispatch for workflow hook evaluation.

The handlers here populate the tracking variables rules depend on. They run
BEFORE rule evaluation so conditions see current data, and a failing observer is
recorded by name rather than aborting the evaluation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from psycopg.errors import QueryCanceled

from gobby.hooks.events import HookEvent, HookEventType
from gobby.storage.hub.operation_deadline import DatabaseOperationDeadlineExceeded

_DATABASE_TIMEOUTS = (DatabaseOperationDeadlineExceeded, QueryCanceled)

logger = logging.getLogger(__name__)


def _is_turn_start_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value == HookEventType.BEFORE_AGENT.value


def _is_turn_end_event(event_type: HookEventType | str) -> bool:
    value = event_type.value if isinstance(event_type, HookEventType) else str(event_type)
    return value in {HookEventType.AFTER_AGENT.value, HookEventType.STOP.value}


def run_observers(
    handler: Any,
    event: HookEvent,
    session_id: str,
    variables: dict[str, Any],
) -> set[str]:
    """Run built-in observer functions to populate tracking variables.

    Must run BEFORE rule evaluation so conditions have current data. Returns the
    names of observers that failed; a database-operation timeout is re-raised so
    the caller's deadline handling still sees it.
    """
    # Imported per call so the observer modules stay a live patch seam (tests
    # replace e.g. ``gobby.workflows.observers.detect_task_claim``) and so this
    # module never participates in an import cycle with ``hooks``.
    from .found_work_gate import capture_found_work_handoff, capture_turn_prompt
    from .observer_context_usage import (
        detect_context_compact_guidance,
        detect_mid_turn_context_compact_guidance,
    )
    from .observer_plan_mode import reconcile_native_mode, resolve_plan_mode
    from .observers import (
        detect_bash_commit,
        detect_commit_link,
        detect_mcp_call,
        detect_task_claim,
        detect_turn_interrupt,
        reconcile_claimed_tasks,
    )

    failures: set[str] = set()

    def run_observer(
        name: str,
        observer: Callable[..., object],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        try:
            observer(*args, **kwargs)
        except _DATABASE_TIMEOUTS:
            raise
        except Exception:
            failures.add(name)
            logger.warning(
                "Observer %s failed for session=%s event=%s",
                name,
                session_id,
                event.event_type,
                exc_info=True,
            )

    run_observer("detect_turn_interrupt", detect_turn_interrupt, event, variables)

    # Tool and stop payloads carry the provider's live permission mode;
    # turn-start events (e.g. Claude UserPromptSubmit) omit it and manual
    # plan-mode toggles fire no hook, so these events are the only
    # authoritative correction point for a stale plan_mode.
    if event.event_type in (
        HookEventType.BEFORE_TOOL,
        HookEventType.AFTER_TOOL,
    ) or _is_turn_end_event(event.event_type):
        run_observer(
            "reconcile_native_mode",
            reconcile_native_mode,
            event,
            variables,
            session_id,
        )

    # SessionStart is the hydration boundary after resume/compaction. Reconcile
    # there so the first tool gate sees authoritative DB claims.
    if event.event_type == HookEventType.SESSION_START or _is_turn_end_event(event.event_type):
        run_observer(
            "reconcile_claimed_tasks",
            reconcile_claimed_tasks,
            variables,
            session_id,
            task_manager=handler._task_manager,
            session_manager=handler._session_manager,
            session_task_manager=handler._session_task_manager,
        )

    # Task claim/release tracking (AFTER_TOOL for gobby-tasks calls)
    if event.event_type == HookEventType.AFTER_TOOL:
        run_observer(
            "detect_task_claim",
            detect_task_claim,
            event,
            variables,
            session_id,
            session_task_manager=handler._session_task_manager,
            task_manager=handler._task_manager,
            project_id=event.project_id,
        )
        run_observer("detect_commit_link", detect_commit_link, event, variables, session_id)
        run_observer("detect_bash_commit", detect_bash_commit, event, variables, session_id)
        run_observer("detect_mcp_call", detect_mcp_call, event, variables, session_id)
        run_observer(
            "capture_found_work_handoff",
            capture_found_work_handoff,
            event,
            variables,
        )
        run_observer(
            "detect_mid_turn_context_compact_guidance",
            detect_mid_turn_context_compact_guidance,
            event,
            variables,
            session_id,
            handler._session_manager,
            config=handler._config_section("context_handoff"),
        )

    # Plan mode detection on the semantic start-of-turn boundary
    if _is_turn_start_event(event.event_type):
        run_observer("capture_turn_prompt", capture_turn_prompt, event, variables)
        run_observer(
            "resolve_plan_mode",
            resolve_plan_mode,
            event,
            variables,
            session_id,
            handler._session_manager,
        )
        run_observer(
            "detect_context_compact_guidance",
            detect_context_compact_guidance,
            variables,
            session_id,
            handler._session_manager,
            config=handler._config_section("context_handoff"),
        )

    return failures
