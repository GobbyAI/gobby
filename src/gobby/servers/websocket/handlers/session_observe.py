"""Session observation handlers for WebSocket session control.

This module remains the public import and monkeypatch surface. Implementation
lives in focused helper modules for continuation, proxy attach/send/detach, and
shared session metadata helpers.
"""

from __future__ import annotations

import logging

from gobby.servers.websocket.handlers.session_observe_continue import (
    _release_source_session,
    check_resume_blocked,
    handle_continue_in_chat,
)
from gobby.servers.websocket.handlers.session_observe_proxy import (
    _resolve_agent_name_for_session,
    handle_attach_to_session,
    handle_detach_from_session,
    handle_send_to_cli_session,
)
from gobby.servers.websocket.handlers.session_observe_support import (
    _as_float,
    _as_int,
    _as_str,
    _can_proxy_attach_session,
    _has_terminal_liveness,
    _is_nonempty_str,
    _is_terminal_session,
    _load_live_session_variables,
    _mode_from_level,
    _normalize_optional_markdown,
    _read_session_variables,
    _resolve_fallback_inject_context,
    _resolve_requested_fallback_context,
    _session_meta_payload,
    _variable_str,
    _variable_value,
)
from gobby.sessions.terminal_kill import kill_terminal_session
from gobby.terminals.lookup import manager_for_terminal_context

logger = logging.getLogger(__name__)

__all__ = [
    "_as_float",
    "_as_int",
    "_as_str",
    "_can_proxy_attach_session",
    "_has_terminal_liveness",
    "_is_nonempty_str",
    "_is_terminal_session",
    "_load_live_session_variables",
    "_mode_from_level",
    "_normalize_optional_markdown",
    "_read_session_variables",
    "_release_source_session",
    "_resolve_agent_name_for_session",
    "_resolve_fallback_inject_context",
    "_resolve_requested_fallback_context",
    "_session_meta_payload",
    "_variable_str",
    "_variable_value",
    "check_resume_blocked",
    "manager_for_terminal_context",
    "handle_attach_to_session",
    "handle_continue_in_chat",
    "handle_detach_from_session",
    "handle_send_to_cli_session",
    "kill_terminal_session",
    "logger",
]
