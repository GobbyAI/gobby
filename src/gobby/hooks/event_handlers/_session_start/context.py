"""Session-start context injection mode helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ContextInjectionMode = Literal["full", "live"]

_CONTEXT_LOSS_SOURCES = {"clear", "compact"}


@dataclass(frozen=True)
class SessionStartContextDecision:
    """Classified context injection mode for a SessionStart event."""

    mode: ContextInjectionMode
    variables: dict[str, Any]
    explicit_context_loss: bool


def classify_session_start_context(
    handler: Any,
    *,
    session_id: str | None,
    session: Any | None,
    session_source: str | None,
    is_existing_session: bool,
) -> SessionStartContextDecision:
    """Decide whether SessionStart should emit full startup or live context."""
    variables = _load_session_variables(handler, session_id)
    explicit_context_loss = _has_explicit_context_loss(session_source, variables)

    if explicit_context_loss or not is_existing_session:
        return SessionStartContextDecision("full", variables, explicit_context_loss)

    if _has_prior_context_evidence(session, variables):
        return SessionStartContextDecision("live", variables, explicit_context_loss)

    return SessionStartContextDecision("full", variables, explicit_context_loss)


def mark_startup_context_injected(handler: Any, session_id: str | None) -> None:
    """Persist markers that full startup context has been emitted."""
    if not session_id or not getattr(handler, "_session_manager", None):
        return

    try:
        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(handler._session_manager.db)
        sv_mgr.merge_variables(session_id, {"_startup_context_injected": True})
    except Exception as e:
        handler.logger.debug(f"Failed to mark startup context variable for {session_id}: {e}")

    update_terminal_pickup_metadata = getattr(
        handler._session_manager,
        "update_terminal_pickup_metadata",
        None,
    )
    if callable(update_terminal_pickup_metadata):
        try:
            update_terminal_pickup_metadata(session_id, context_injected=True)
        except Exception as e:
            handler.logger.debug(f"Failed to mark startup context row for {session_id}: {e}")


def _load_session_variables(handler: Any, session_id: str | None) -> dict[str, Any]:
    if not session_id or not getattr(handler, "_session_manager", None):
        return {}

    try:
        from gobby.workflows.state_manager import SessionVariableManager

        variables = SessionVariableManager(handler._session_manager.db).get_variables(session_id)
    except Exception as e:
        handler.logger.debug(f"Failed to load session variables for {session_id}: {e}")
        return {}

    return variables if isinstance(variables, dict) else {}


def _has_explicit_context_loss(
    session_source: str | None,
    variables: dict[str, Any],
) -> bool:
    source = (session_source or "startup").lower()
    if source in _CONTEXT_LOSS_SOURCES:
        return True
    return source == "resume" and variables.get("pending_context_reset") is True


def _has_prior_context_evidence(session: Any | None, variables: dict[str, Any]) -> bool:
    if variables.get("_startup_context_injected") is True:
        return True
    if variables.get("_agent_context_injected") is True:
        return True
    if session is None:
        return False
    if getattr(session, "context_injected", False) is True:
        return True
    return _positive_count(getattr(session, "message_count", 0)) or _positive_count(
        getattr(session, "turn_count", 0)
    )


def _positive_count(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.isdigit():
        return int(value) > 0
    return False
