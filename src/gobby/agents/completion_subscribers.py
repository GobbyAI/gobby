"""Shared helpers for agent completion wake subscribers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


class SubscriptionPersistenceError(RuntimeError):
    """Raised when a strict completion subscription cannot be persisted."""


@dataclass(frozen=True, slots=True)
class AgentCompletionSubscription:
    """Outcome of registering subscribers for an agent completion event."""

    subscribers: list[str]
    created_fresh_entry: bool
    inserted_session_ids: list[str]


def completion_subscriber_lineage(
    session_id: str,
    session_manager: SessionManager | None,
) -> list[str]:
    """Return root-to-session subscriber ids for wake delivery.

    If lineage support, session lookup, or unexpected lineage resolution fails,
    the requested session remains the only subscriber and the failure is logged
    so completion wakeup remains best-effort.
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
    except psycopg.DatabaseError as e:
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
    return _dedupe(lineage_ids)


def subscribe_agent_completion(
    *,
    completion_registry: CompletionEventRegistry | None,
    run_id: str,
    subscriber_session_id: str,
    session_manager: SessionManager | None = None,
    db: HubDatabase | None = None,
    strict: bool = False,
) -> AgentCompletionSubscription:
    """Register in-memory and durable subscribers for an agent completion event."""
    subscribers = completion_subscriber_lineage(subscriber_session_id, session_manager)
    created_fresh_entry = False
    inserted_session_ids: list[str] = []

    if strict:
        if db is None:
            raise SubscriptionPersistenceError(
                f"Cannot persist completion subscribers for run {run_id}: database unavailable"
            )
        try:
            from gobby.storage.pipeline_subscribers import CompletionSubscriberManager

            manager = CompletionSubscriberManager(db=db)
            inserted_session_ids = manager.add_completion_subscribers(run_id, subscribers)
        except (ImportError, psycopg.DatabaseError) as exc:
            raise SubscriptionPersistenceError(
                f"Failed to persist completion subscribers for run {run_id}"
            ) from exc
        if completion_registry is not None:
            created_fresh_entry = completion_registry.register(run_id, subscribers=subscribers)
    else:
        if completion_registry is not None:
            created_fresh_entry = completion_registry.register(run_id, subscribers=subscribers)
        if db is not None:
            try:
                from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
            except ImportError:
                logger.debug("Could not load CompletionSubscriberManager", exc_info=True)
            else:
                manager = CompletionSubscriberManager(db=db)
                try:
                    inserted_session_ids = manager.add_completion_subscribers(run_id, subscribers)
                except psycopg.DatabaseError:
                    logger.debug(
                        "Failed to persist completion subscribers for run %s",
                        run_id,
                        exc_info=True,
                    )

    return AgentCompletionSubscription(
        subscribers=subscribers,
        created_fresh_entry=created_fresh_entry,
        inserted_session_ids=inserted_session_ids,
    )


def remove_agent_completion_subscribers(
    *,
    db: HubDatabase,
    run_id: str,
    session_ids: list[str] | None = None,
) -> None:
    """Remove all or selected durable completion subscribers for an agent run."""
    try:
        from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
    except ImportError:
        logger.debug("Could not load CompletionSubscriberManager", exc_info=True)
        return

    manager = CompletionSubscriberManager(db=db)
    try:
        if session_ids is None:
            manager.remove_completion_subscribers(run_id)
        else:
            manager.remove_completion_subscribers(run_id, session_ids=session_ids)
    except psycopg.DatabaseError:
        logger.debug(
            "Failed to remove completion subscribers for run %s",
            run_id,
            exc_info=True,
        )


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


__all__ = [
    "completion_subscriber_lineage",
    "remove_agent_completion_subscribers",
    "subscribe_agent_completion",
]
