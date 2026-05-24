"""
Resolution utilities for workflow tools.

Provides functions to resolve session and task references from various
formats (#N, N, UUID, prefix) to canonical UUIDs.
"""

import logging
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks._id import resolve_task_reference
from gobby.storage.tasks._models import TaskNotFoundError
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)


def resolve_session_id(session_manager: SessionManager, ref: str) -> str:
    """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
    project_ctx = get_project_context()
    project_id = project_ctx.get("id") if project_ctx else None
    return session_manager.resolve_session_reference(ref, project_id)


def resolve_session_task_value(
    value: Any,
    session_id: str | None,
    session_manager: SessionManager,
    db: HubDatabase,
) -> Any:
    """
    Resolve a session_task value from seq_num reference (#N or N) to UUID.

    This prevents repeated resolution failures in condition evaluation when
    task_tree_complete() is called with a seq_num that requires project_id.

    Args:
        value: The value to potentially resolve (e.g., "#4424", "47", or a UUID)
        session_id: Session ID to look up project_id
        session_manager: Session manager for lookups
        db: Database for task resolution

    Returns:
        Resolved UUID if value was a seq_num reference, otherwise original value
    """
    # Only process string values that look like seq_num references
    if not isinstance(value, str):
        return value

    # Check if it's a seq_num reference (#N or plain N)
    is_seq_ref = value.startswith("#") or value.isdigit()
    if not is_seq_ref:
        return value

    # Need session to get project_id
    if not session_id:
        logger.warning(f"Cannot resolve task reference '{value}': no session_id provided")
        return value

    # Get project_id from session
    session = session_manager.get(session_id)
    if not session or not session.project_id:
        logger.warning(f"Cannot resolve task reference '{value}': session has no project_id")
        return value

    # Resolve the reference
    try:
        resolved = resolve_task_reference(db, value, session.project_id)
        logger.debug(f"Resolved session_task '{value}' to UUID '{resolved}'")
        return resolved
    except TaskNotFoundError as e:
        logger.warning(f"Could not resolve task reference '{value}': {e}")
        return value
    except Exception as e:
        logger.warning(f"Unexpected error resolving task reference '{value}': {e}")
        return value
