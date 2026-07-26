"""Build-coordinator completion subscriptions for dispatcher-spawned agents."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from gobby.agents.completion_subscribers import subscribe_agent_completion
from gobby.build.coordinator import summary_allows_cross_project_coordinator
from gobby.storage.build_history import BuildHistoryStorage
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

__all__ = [
    "_coordinator_session_matches_project",
    "_subscribe_build_coordinator_completion",
    "subscribe_agent_completion",
]


class BuildCompletionServices(Protocol):
    session_manager: SessionManager | None
    completion_registry: CompletionEventRegistry | None


def _subscribe_build_coordinator_completion(
    *,
    db: HubDatabase,
    project_id: str,
    task_id: str,
    run_id: str,
    services: BuildCompletionServices | None,
) -> None:
    """Subscribe the active build coordinator, if any, to agent completion."""
    run = BuildHistoryStorage(db).latest_coordinated_run_for_task(project_id, task_id)
    if run is None or not run.summary:
        return
    coordinator_session_id = run.summary.get("coordinator_session_id")
    if not isinstance(coordinator_session_id, str) or not coordinator_session_id:
        return
    if services is None:
        logger.debug(
            "Skipping build coordinator completion subscription; no services",
            extra={"coordinator_session_id": coordinator_session_id, "project_id": project_id},
        )
        return
    session_manager = services.session_manager
    if not _coordinator_session_matches_project(
        session_manager,
        coordinator_session_id,
        project_id,
        run.summary,
    ):
        return
    subscribe_agent_completion(
        completion_registry=services.completion_registry,
        run_id=run_id,
        subscriber_session_id=coordinator_session_id,
        db=db,
    )


def _coordinator_session_matches_project(
    session_manager: SessionManager | None,
    coordinator_session_id: str,
    project_id: str,
    run_summary: dict[str, object],
) -> bool:
    """Return whether a coordinator session exists and is authorized for this build."""
    if session_manager is None:
        logger.debug(
            "Skipping build coordinator completion subscription; no session_manager",
            extra={"coordinator_session_id": coordinator_session_id, "project_id": project_id},
        )
        return False
    session = session_manager.get(coordinator_session_id)
    if session is None:
        logger.debug(
            "Skipping build coordinator completion subscription; coordinator session missing",
            extra={"coordinator_session_id": coordinator_session_id, "project_id": project_id},
        )
        return False
    coordinator_project_id = getattr(session, "project_id", None)
    if coordinator_project_id != project_id and not summary_allows_cross_project_coordinator(
        run_summary,
        coordinator_project_id=coordinator_project_id,
        build_project_id=project_id,
    ):
        logger.warning(
            "Skipping build coordinator completion subscription for cross-project session",
            extra={
                "coordinator_session_id": coordinator_session_id,
                "coordinator_project_id": coordinator_project_id,
                "project_id": project_id,
            },
        )
        return False
    return True
