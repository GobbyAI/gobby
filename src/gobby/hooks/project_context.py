"""Project context resolution helpers for hook events."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.hooks.events import HookEvent
from gobby.hooks.session_types import HookSessionManager


@dataclass(frozen=True)
class HookProjectResolution:
    project_id: str | None
    source: str | None = None
    skipped: bool = False
    reason: str | None = None


def is_unusable_hook_cwd(cwd: str | None) -> bool:
    """Return True when cwd is a runtime artifact rather than project context."""
    if not cwd:
        return False
    path = Path(cwd).expanduser()
    return path.is_absolute() and path.parent == path


def apply_project_id_to_event(event: HookEvent, project_id: str) -> None:
    """Persist recovered project context on both normalized event surfaces."""
    event.project_id = project_id
    event.data["project_id"] = project_id


def resolve_hook_project_context(
    event: HookEvent,
    *,
    session_manager: HookSessionManager | None,
    resolve_project_id: Callable[[str | None, str | None], str],
    logger: logging.Logger | None = None,
) -> HookProjectResolution:
    """Resolve project context for hooks without treating cwd=/ as a project."""
    explicit_project_id = _as_nonempty_str(event.project_id) or _as_nonempty_str(
        event.data.get("project_id")
    )
    if explicit_project_id:
        apply_project_id_to_event(event, explicit_project_id)
        return HookProjectResolution(explicit_project_id, source="explicit")

    for source, session_id in _candidate_session_ids(event):
        project_id = _project_id_from_session(session_manager, session_id, logger)
        if project_id:
            apply_project_id_to_event(event, project_id)
            return HookProjectResolution(project_id, source=source)

    project_id = _project_id_from_existing_session(event, session_manager, logger)
    if project_id:
        apply_project_id_to_event(event, project_id)
        return HookProjectResolution(project_id, source="existing-session")

    project_id = _project_id_from_current_context()
    if project_id:
        apply_project_id_to_event(event, project_id)
        return HookProjectResolution(project_id, source="current-context")

    cwd = _as_nonempty_str(event.cwd) or _as_nonempty_str(event.data.get("cwd"))
    if is_unusable_hook_cwd(cwd):
        return HookProjectResolution(
            None,
            skipped=True,
            reason=f"unusable hook cwd: {cwd}",
        )

    project_id = resolve_project_id(None, cwd)
    apply_project_id_to_event(event, project_id)
    return HookProjectResolution(project_id, source="cwd")


def _candidate_session_ids(event: HookEvent) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    platform_session_id = _as_nonempty_str(event.metadata.get("_platform_session_id"))
    if platform_session_id:
        candidates.append(("platform-session", platform_session_id))

    terminal_context = event.data.get("terminal_context")
    if isinstance(terminal_context, dict):
        gobby_session_id = _as_nonempty_str(terminal_context.get("gobby_session_id"))
        if gobby_session_id:
            candidates.append(("terminal-context", gobby_session_id))

    return candidates


def _project_id_from_session(
    session_manager: HookSessionManager | None,
    session_id: str,
    logger: logging.Logger | None,
) -> str | None:
    if session_manager is None:
        return None
    try:
        session = session_manager.get(session_id)
    except Exception as exc:
        if logger:
            logger.debug("Failed to resolve hook session %s: %s", session_id, exc)
        return None
    return _as_nonempty_str(getattr(session, "project_id", None))


def _project_id_from_existing_session(
    event: HookEvent,
    session_manager: HookSessionManager | None,
    logger: logging.Logger | None,
) -> str | None:
    if session_manager is None or not event.session_id:
        return None

    try:
        cached_session_id = session_manager.get_session_id(event.session_id, event.source.value)
    except Exception as exc:
        if logger:
            logger.debug("Failed to read hook session cache for %s: %s", event.session_id, exc)
        cached_session_id = None
    if cached_session_id:
        project_id = _project_id_from_session(session_manager, cached_session_id, logger)
        if project_id:
            return project_id

    finder = getattr(session_manager, "find_active_by_external_id", None)
    if callable(finder):
        try:
            session = finder(event.session_id, event.source.value)
        except Exception as exc:
            if logger:
                logger.debug("Failed active hook session lookup for %s: %s", event.session_id, exc)
        else:
            project_id = _as_nonempty_str(getattr(session, "project_id", None))
            if project_id:
                return project_id

    try:
        recovered = session_manager.recover_session(
            external_id=event.session_id,
            source=event.source.value,
            machine_id=event.machine_id or "",
            project_id=None,
        )
    except Exception as exc:
        if logger:
            logger.debug("Failed hook session recovery for %s: %s", event.session_id, exc)
        return None
    return _as_nonempty_str(getattr(recovered, "project_id", None))


def _project_id_from_current_context() -> str | None:
    from gobby.utils.project_context import get_project_context

    context = get_project_context(None)
    if not context:
        return None
    return _as_nonempty_str(context.get("id"))


def _as_nonempty_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
