"""Shared helpers for websocket session observation handlers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypeGuard

from gobby.servers.websocket.db import run_db
from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)

_VALID_FALLBACK_CONTEXTS = {"auto", "summary", "handoff", "none"}


def _is_terminal_session(session: Any) -> bool:
    """Return True when a session supports live attach/proxy semantics."""
    return getattr(session, "session_type", None) == "terminal"


def _as_str(value: Any) -> str | None:
    """Return a JSON-safe string or None."""
    return value if isinstance(value, str) else None


def _normalize_optional_markdown(value: str | None) -> str | None:
    """Return original markdown text unless it is missing or whitespace-only."""
    if not value or not value.strip():
        return None
    return value


def _as_int(value: Any, default: int | None = None) -> int | None:
    """Return a JSON-safe int or the provided default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except ValueError:
            return default
    return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Return a JSON-safe float or the provided default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return float(stripped)
        except ValueError:
            return default
    return default


def _mode_from_level(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return {0: "plan", 1: "normal", 2: "bypass"}.get(value)
    return None


def _variable_value[T](
    variables: dict[str, Any],
    predicate: Callable[[Any], TypeGuard[T]],
    *names: str,
) -> T | None:
    for name in names:
        value = variables.get(name)
        if predicate(value):
            return value
    return None


def _is_nonempty_str(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _variable_str(variables: dict[str, Any], *names: str) -> str | None:
    return _variable_value(variables, _is_nonempty_str, *names)


def _read_session_variables(db: Any, session_id: str) -> dict[str, Any]:
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db).get_variables(session_id)
    return variables if isinstance(variables, dict) else {}


async def _load_live_session_variables(
    mixin: SessionControlMixin,
    session_manager: Any,
    session_id: str,
) -> dict[str, Any]:
    db = getattr(session_manager, "db", None) or getattr(mixin, "db", None)
    if db is None:
        return {}
    try:
        variables = await run_db(mixin, _read_session_variables, db, session_id)
        return variables if isinstance(variables, dict) else {}
    except Exception as exc:
        logger.debug("Failed to read live session variables for %s: %s", session_id, exc)
        return {}


def _has_terminal_liveness(terminal_context: Any) -> bool:
    """Return True when terminal metadata indicates a live tmux-backed session."""
    if not isinstance(terminal_context, dict):
        return False

    tmux_pane = terminal_context.get("tmux_pane")
    if isinstance(tmux_pane, str) and tmux_pane:
        return True

    parent_pid = terminal_context.get("parent_pid")
    if isinstance(parent_pid, int) and not isinstance(parent_pid, bool):
        return parent_pid > 0
    if isinstance(parent_pid, str):
        try:
            return int(parent_pid) > 0
        except ValueError:
            return False

    return False


def _can_proxy_attach_session(session: Any) -> bool:
    """Return True when a terminal session is eligible for interactive proxy attach."""
    if getattr(session, "session_type", None) != "terminal":
        return False
    if getattr(session, "status", None) not in TERMINAL_OWNER_STATUSES:
        return False

    explicit = getattr(session, "can_proxy_attach", None)
    if isinstance(explicit, bool):
        return explicit

    return _has_terminal_liveness(getattr(session, "terminal_context", None))


def _session_meta_payload(
    session: Any,
    *,
    variables: dict[str, Any],
    agent_name: str | None,
    workflow_name: str | None,
    agent_run_id: str | None,
    context_window: int | None = None,
) -> dict[str, Any]:
    seq_num = _as_int(getattr(session, "seq_num", None))
    live_chat_mode = (
        _variable_str(variables, "chat_mode")
        or _mode_from_level(variables.get("mode_level"))
        or _as_str(getattr(session, "chat_mode", None))
    )
    live_reasoning_effort = _variable_str(
        variables,
        "reasoning_effort",
        "_effective_reasoning_effort",
        "_requested_reasoning_effort",
    ) or _as_str(getattr(session, "reasoning_effort", None))
    live_model = _variable_str(
        variables,
        "model",
        "model_id",
        "modelId",
    ) or _as_str(getattr(session, "model", None))
    live_context_window = context_window

    return {
        "external_id": _as_str(getattr(session, "external_id", None)),
        "source": _as_str(getattr(session, "source", None)) or "unknown",
        "title": _as_str(getattr(session, "title", None)),
        "status": _as_str(getattr(session, "status", None)) or "unknown",
        "model": live_model,
        "reasoning_effort": live_reasoning_effort,
        "ref": f"#{seq_num}" if seq_num is not None else None,
        "chat_mode": live_chat_mode,
        "git_branch": _as_str(getattr(session, "git_branch", None)),
        "context_window": live_context_window,
        "session_type": _as_str(getattr(session, "session_type", None)),
        "can_proxy_attach": _can_proxy_attach_session(session),
        "workflow_name": workflow_name,
        "agent_run_id": agent_run_id,
        "agent_name": agent_name,
        "context_used_tokens": _as_int(getattr(session, "context_used_tokens", None)),
        "context_usage_ratio": _as_float(getattr(session, "context_usage_ratio", None)),
        "context_usage_source": _as_str(getattr(session, "context_usage_source", None)),
        "context_usage_confidence": _as_str(
            getattr(session, "context_usage_confidence", None),
        ),
        "last_prompt_input_tokens": _as_int(
            getattr(session, "last_prompt_input_tokens", None),
        ),
        "last_prompt_uncached_input_tokens": _as_int(
            getattr(session, "last_prompt_uncached_input_tokens", None),
        ),
        "last_prompt_cache_read_tokens": _as_int(
            getattr(session, "last_prompt_cache_read_tokens", None),
        ),
        "last_prompt_cache_creation_tokens": _as_int(
            getattr(session, "last_prompt_cache_creation_tokens", None),
        ),
        "last_completion_output_tokens": _as_int(
            getattr(session, "last_completion_output_tokens", None),
        ),
        "usage_input_tokens": _as_int(getattr(session, "usage_input_tokens", 0), 0),
        "usage_output_tokens": _as_int(getattr(session, "usage_output_tokens", 0), 0),
        "usage_cache_read_tokens": _as_int(getattr(session, "usage_cache_read_tokens", 0), 0),
        "usage_cache_creation_tokens": _as_int(
            getattr(session, "usage_cache_creation_tokens", 0),
            0,
        ),
    }


def _resolve_requested_fallback_context(data: dict[str, Any]) -> str:
    """Return the requested fallback context mode, defaulting to auto."""
    requested = _as_str(data.get("fallback_context")) or _as_str(data.get("fallbackContext"))
    if requested in _VALID_FALLBACK_CONTEXTS:
        return requested
    return "auto"


def _resolve_fallback_inject_context(source_session: Any, requested_mode: str) -> str | None:
    """Choose hidden resume context based on the requested fallback mode."""
    if requested_mode == "none" or not source_session:
        return None

    summary_markdown = _normalize_optional_markdown(
        _as_str(getattr(source_session, "summary_markdown", None))
    )
    handoff_markdown = _normalize_optional_markdown(
        _as_str(getattr(source_session, "handoff_markdown", None))
    )

    if requested_mode == "summary":
        return summary_markdown
    if requested_mode == "handoff":
        return handoff_markdown

    return summary_markdown or handoff_markdown
