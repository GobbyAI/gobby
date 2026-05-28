"""Build coordinator resolution and history metadata helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from gobby.build.options import BuildOptions
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager


@dataclass(frozen=True)
class BuildCoordinator:
    """Resolved build coordinator session identity."""

    session_id: str
    project_id: str | None


def resolve_build_coordinator(
    opts: BuildOptions,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None,
) -> BuildCoordinator | None:
    """Resolve and validate the build coordinator session for a build request."""
    ref = opts.coordinator_session_ref
    if not ref:
        return None
    manager = getattr(services, "session_manager", None) or SessionManager(db)
    try:
        resolved_id = str(manager.resolve_session_reference(ref, project_id))
    except ValueError as exc:
        raise ValueError(f"build coordinator session could not be resolved: {exc}") from exc
    session = manager.get(resolved_id)
    if session is None:
        raise ValueError(f"build coordinator session not found: {ref}")
    coordinator_project_id = getattr(session, "project_id", None)
    if coordinator_project_id == project_id:
        return BuildCoordinator(session_id=resolved_id, project_id=coordinator_project_id)
    if opts.project_explicit and _is_primary_session_uuid(ref, resolved_id):
        return BuildCoordinator(session_id=resolved_id, project_id=coordinator_project_id)
    raise ValueError(
        "build coordinator session must belong to the build project; "
        "use --project with --coordinator current or a full session UUID for "
        "cross-project coordination"
    )


def build_run_summary(
    payload: dict[str, object],
    *,
    coordinator: BuildCoordinator | None,
    build_project_id: str,
) -> dict[str, object]:
    """Attach build/coordinator project metadata to a build history summary."""
    summary: dict[str, object] = {**payload, "build_project_id": build_project_id}
    if coordinator is not None:
        summary["coordinator_session_id"] = coordinator.session_id
        summary["coordinator_project_id"] = coordinator.project_id
    return summary


def summary_allows_cross_project_coordinator(
    summary: dict[str, Any],
    *,
    coordinator_project_id: str | None,
    build_project_id: str,
) -> bool:
    """Return whether a run summary explicitly authorizes cross-project coordination."""
    return (
        summary.get("build_project_id") == build_project_id
        and summary.get("coordinator_project_id") == coordinator_project_id
    )


def _is_primary_session_uuid(ref: str, resolved_id: str) -> bool:
    try:
        return str(uuid.UUID(ref)) == resolved_id
    except ValueError:
        return False


__all__ = [
    "BuildCoordinator",
    "build_run_summary",
    "resolve_build_coordinator",
    "summary_allows_cross_project_coordinator",
]
