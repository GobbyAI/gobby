"""Force-new web-chat row binding for clear_self successors."""

from __future__ import annotations

import logging
from typing import Any

from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.db import run_db
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.machine_id import get_machine_id

from ._session_binding import _normalize_web_chat_provider

logger = logging.getLogger(__name__)


def wire_db_persist_callbacks(mixin: Any, session: ChatSessionProtocol) -> None:
    """Point mode/tool persistence callbacks at the session's current db id."""
    session_manager = getattr(mixin, "session_manager", None)
    db_session_id = getattr(session, "db_session_id", None)
    if session_manager is None or not isinstance(db_session_id, str) or not db_session_id:
        return

    def _persist_mode(mode: str) -> None:
        try:
            session_manager.update_chat_mode(db_session_id, mode)
        except Exception:
            logger.debug("Failed to persist chat_mode", exc_info=True)

    def _persist_approved_tools(tools: set[str]) -> None:
        try:
            session_manager.update_approved_tools(db_session_id, tools)
        except Exception:
            logger.debug("Failed to persist approved_tools", exc_info=True)

    session._on_mode_persist = _persist_mode
    session._on_approved_tools_persist = _persist_approved_tools


def rebind_live_clear_successor(mixin: Any, session: ChatSessionProtocol, successor: Any) -> None:
    """Point the live wrapper at the successor row without starting a backend."""
    session.db_session_id = successor.id
    session.seq_num = successor.seq_num
    session.message_index = 0
    wire_db_persist_callbacks(mixin, session)


async def bind_force_new_reuse_session(
    mixin: Any,
    session: ChatSessionProtocol,
    conversation_id: str,
    *,
    model: str | None,
    provider: str | None,
) -> ChatSessionProtocol:
    """Insert a bootstrap web-chat row and rebind an already-running wrapper."""
    del conversation_id
    session_manager = getattr(mixin, "session_manager", None)
    if session_manager is None:
        return session

    project_id = getattr(session, "project_id", None)
    if not isinstance(project_id, str) or not project_id:
        project_id = PERSONAL_PROJECT_ID
    source = (
        _normalize_web_chat_provider(provider) or getattr(session, "provider", None) or "claude"
    )
    policy_hash = getattr(session, "sandbox_policy_hash", None)
    if not isinstance(policy_hash, str):
        runtime_manager = getattr(mixin, "web_chat_runtime_manager", None)
        raw_policy = getattr(runtime_manager, "sandbox_policy_hash", None)
        policy_hash = raw_policy if isinstance(raw_policy, str) else ""
    sandbox_enabled = getattr(session, "sandbox_metadata", None)
    enforced = False
    if isinstance(sandbox_enabled, dict) and isinstance(sandbox_enabled.get("enforced"), bool):
        enforced = sandbox_enabled["enforced"]
    chat_mode = getattr(session, "chat_mode", None)
    if not isinstance(chat_mode, str):
        chat_mode = None
    selected_model = model if isinstance(model, str) and model else getattr(session, "model", None)
    if not isinstance(selected_model, str):
        selected_model = None

    created = await run_db(
        mixin,
        session_manager.create_web_chat_session,
        machine_id=get_machine_id(),
        project_id=project_id,
        source=source,
        sandbox_enabled=enforced,
        sandbox_policy_hash=policy_hash,
        model=selected_model,
        chat_mode=chat_mode,
    )
    rebind_live_clear_successor(mixin, session, created)
    return session
