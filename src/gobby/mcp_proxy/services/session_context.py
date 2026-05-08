"""Session and discovery-state helpers for the tool proxy service."""

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger("gobby.mcp.server")


def get_requested_session_id(session_id: str | None) -> str | None:
    """Return the explicit session reference or the current session context UUID."""
    if session_id:
        return session_id

    from gobby.utils.session_context import get_session_context

    ctx = get_session_context()
    return ctx.session_id if ctx else None


def get_effective_session_id(service: Any, session_id: str | None) -> str | None:
    """Return the resolved platform session UUID for a session reference."""
    return cast("str | None", service._resolve_platform_session_id(session_id))


def resolve_hook_manager(service: Any) -> "HookManager | None":
    """Resolve HookManager lazily to avoid startup-order cycles."""
    if service._hook_manager_resolver is None:
        return None

    try:
        return cast("HookManager | None", service._hook_manager_resolver())
    except Exception as exc:
        logger.debug(f"Failed to resolve HookManager for tool enforcement: {exc}")
        return None


def resolve_platform_session_id(service: Any, session_id: str | None) -> str | None:
    """Resolve the best available session reference to a platform session UUID."""
    requested_session_id = cast("str | None", service._get_requested_session_id(session_id))
    if not requested_session_id:
        return None

    hook_manager = service._resolve_hook_manager()
    session_manager = getattr(hook_manager, "_session_manager", None) if hook_manager else None
    if session_manager is None:
        return requested_session_id

    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context()
    project_id = project_ctx.get("id") if project_ctx else None
    try:
        resolved_session_id = cast(
            "str | None",
            session_manager.resolve_session_reference(requested_session_id, project_id),
        )
    except ValueError as exc:
        logger.warning(
            "Could not resolve session reference %r (project_id=%s): %s",
            requested_session_id,
            project_id,
            exc,
        )
        return None
    return resolved_session_id or requested_session_id


def resolve_tool_event_context(
    service: Any,
    effective_session_id: str,
) -> tuple[
    "HookManager | None", Any | None, Any | None, Any, dict[str, Any], str | None, str | None
]:
    """Resolve shared session metadata for direct tool lifecycle events."""
    from gobby.hooks.events import SessionSource
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context()
    cwd = project_ctx.get("project_path") if project_ctx else None
    project_id = project_ctx.get("id") if project_ctx else None
    source = SessionSource.CODEX
    metadata: dict[str, Any] = {"_platform_session_id": effective_session_id}

    hook_manager = service._resolve_hook_manager()
    session_storage = getattr(hook_manager, "_session_manager", None) if hook_manager else None
    session = None
    if session_storage is not None:
        try:
            session = session_storage.get(effective_session_id)
        except Exception as exc:
            logger.warning(
                "Failed to load session %s for tool event; source will default to codex: %s",
                effective_session_id,
                exc,
                exc_info=True,
            )
        else:
            if session is not None:
                session_source = getattr(session, "source", None)
                if isinstance(session_source, str):
                    try:
                        source = SessionSource(session_source)
                    except ValueError:
                        logger.debug(
                            "Unknown session source %r for %s; defaulting to codex",
                            session_source,
                            effective_session_id,
                        )
                project_id = project_id or getattr(session, "project_id", None)
                external_id = getattr(session, "external_id", None)
                if external_id:
                    metadata["external_id"] = external_id

    if cwd:
        metadata["project_path"] = cwd

    return hook_manager, session_storage, session, source, metadata, cwd, project_id


def record_discovery_state(
    service: Any,
    session_id: str | None,
    *,
    servers_listed: bool = False,
    listed_server: str | None = None,
) -> None:
    """Persist discovery state directly for proxy-executed discovery calls."""
    resolved_session_id = service._resolve_platform_session_id(session_id)
    if not resolved_session_id:
        return

    hook_manager = service._resolve_hook_manager()
    db = getattr(hook_manager, "_database", None) if hook_manager else None
    if db is None:
        return

    try:
        from gobby.workflows.state_manager import SessionVariableManager

        session_var_manager = SessionVariableManager(db)
        if servers_listed:
            session_var_manager.set_variable(resolved_session_id, "servers_listed", True)
        if listed_server:
            session_var_manager.append_to_set_variable(
                resolved_session_id, "listed_servers", [listed_server]
            )
    except Exception as exc:
        logger.debug("Failed to record discovery state for %s: %s", resolved_session_id, exc)
