"""Shared helpers for agent completion wake subscribers."""

from __future__ import annotations

import logging
import sqlite3
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
    """Return root-to-session subscriber ids for wake delivery.

    If lineage support or session lookup is unavailable, the requested session
    remains the only subscriber so completion wakeup remains best-effort.
    """
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
    except (sqlite3.DatabaseError, psycopg.Error) as e:
        logger.warning(
            "Could not resolve session lineage for %s: %s",
            session_id,
            e,
            exc_info=True,
        )
    except Exception as e:
        logger.warning(
            "Unexpected error resolving session lineage for %s: %s",
            session_id,
            e,
            exc_info=True,
        )
        raise
    return _dedupe(lineage_ids)


def subscribe_agent_completion(
    *,
    completion_registry: CompletionEventRegistry | None,
    run_id: str,
    subscriber_session_id: str,
    session_manager: SessionManager | None = None,
    db: HubDatabase | None = None,
) -> list[str]:
    """Register in-memory and durable subscribers for an agent completion event."""
    subscribers = completion_subscriber_lineage(subscriber_session_id, session_manager)
    if completion_registry is not None:
        try:
            completion_registry.register(run_id, subscribers=subscribers)
        except Exception as e:
            # completion_registry.register(run_id, subscribers) is best-effort wake wiring.
            logger.warning(
                "Failed to register completion event for run %s with subscribers %s: %s",
                run_id,
                subscribers,
                e,
                exc_info=True,
            )
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
    """Return values with duplicates removed while preserving order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


__all__ = ["completion_subscriber_lineage", "subscribe_agent_completion"]
