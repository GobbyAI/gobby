"""Shared helpers for resolving session references in tool inputs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.hooks.session_types import HookSessionManager

logger = logging.getLogger(__name__)


def try_resolve_session_field(
    container: dict[str, Any],
    field: str,
    *,
    session_manager: HookSessionManager | None,
    project_id: str | None,
) -> bool:
    """Resolve a #N session reference in container[field] to a UUID in place."""
    if session_manager is None:
        return False

    value = container.get(field)
    if not isinstance(value, str):
        return False

    ref = value.lstrip("#") if value.startswith("#") else value
    if not ref.isdigit():
        return False

    try:
        resolved = session_manager.resolve_session_reference(value, project_id)
    except ValueError as exc:
        logger.debug("Could not resolve session ref %r: %s", value, exc)
        return False
    except Exception as exc:
        logger.warning(
            "Unexpected error resolving session ref %r: %s",
            value,
            exc,
            exc_info=True,
        )
        return False

    if resolved == value:
        return False

    container[field] = resolved
    return True
