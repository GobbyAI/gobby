"""Shared helpers for agent completion wake subscribers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def completion_subscriber_lineage(
    session_id: str,
    session_manager: SessionManager | None,
) -> list[str]:
    """Return root-to-session subscriber ids, falling back to the given session."""
    lineage_ids = [session_id]
    if session_manager is None:
        return lineage_ids
    try:
        from gobby.agents.session import ChildSessionManager
    except (ImportError, AttributeError):
        logger.debug("Could not load child session lineage support", exc_info=True)
        return _dedupe(lineage_ids)

    try:
        lineage = ChildSessionManager(session_manager).get_session_lineage(session_id)
        lineage_ids = [str(session.id) for session in lineage]
        if session_id not in lineage_ids:
            lineage_ids.append(session_id)
    except (AttributeError, ValueError, RuntimeError):
        logger.debug("Could not resolve session lineage for %s", session_id, exc_info=True)
    return _dedupe(lineage_ids)


def subscribe_agent_completion(
    *,
    completion_registry: CompletionEventRegistry | None,
    run_id: str,
    subscriber_session_id: str,
    session_manager: SessionManager | None = None,
    db: HubDatabase | None = None,
) -> list[str]:
    """Register an agent run completion event and persist its subscriber lineage."""
    subscribers = completion_subscriber_lineage(subscriber_session_id, session_manager)
    if completion_registry is not None:
        try:
            completion_registry.register(run_id, subscribers=subscribers)
        except TypeError:
            logger.debug("Failed to register completion event for run %s", run_id, exc_info=True)
            return subscribers

    if db is not None:
        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager
        except ImportError:
            logger.debug("Could not load pipeline execution manager", exc_info=True)
        else:
            try:
                manager = LocalPipelineExecutionManager(db=db, project_id="")
                manager.add_completion_subscribers(run_id, subscribers)
            except (ValueError, psycopg.Error):
                logger.debug(
                    "Failed to persist completion subscribers for run %s",
                    run_id,
                    exc_info=True,
                )

    return subscribers


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


__all__ = ["completion_subscriber_lineage", "subscribe_agent_completion"]
