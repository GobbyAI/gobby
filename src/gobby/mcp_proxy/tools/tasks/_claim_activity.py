"""Session-liveness guard shared by task-claiming MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from gobby.mcp_proxy.tools.tasks._context import RegistryContext

logger = logging.getLogger(__name__)


def confirm_claiming_session_activity(
    ctx: RegistryContext,
    session_id: str,
    session: Any | None,
) -> bool:
    """Mark a present current session active before assigning task ownership.

    Confirmed MCP activity may legitimately revive a handoff-ready or expired
    session. A missing session keeps the existing graceful-degradation path;
    callers already resolved its reference and can still let storage decide.
    """
    if session is None:
        return True

    try:
        return ctx.session_manager.update_session_status(
            session_id,
            "active",
            activity_confirmed=True,
        )
    except Exception:
        logger.exception("Failed to confirm activity for claiming session %s", session_id)
        return False
