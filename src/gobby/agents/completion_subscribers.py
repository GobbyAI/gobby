"""Shared helpers for agent completion wake subscribers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class SubscriptionPersistenceError(RuntimeError):
    """Raised when a strict completion subscription cannot be persisted."""


@dataclass(frozen=True, slots=True)
class AgentCompletionSubscription:
    """Outcome of registering subscribers for an agent completion event."""

    subscribers: list[str]
    created_fresh_entry: bool
    inserted_session_ids: list[str]


def subscribe_agent_completion(
    *,
    completion_registry: CompletionEventRegistry | None,
    run_id: str,
    subscriber_session_id: str,
    db: HubDatabase | None = None,
    strict: bool = False,
) -> AgentCompletionSubscription:
    """Register in-memory and durable subscribers for an agent completion event."""
    subscribers = [subscriber_session_id]
    created_fresh_entry = False
    inserted_session_ids: list[str] = []

    def local_persist(target_db: HubDatabase) -> list[str]:
        from gobby.storage.pipeline_subscribers import CompletionSubscriberManager

        manager = CompletionSubscriberManager(db=target_db)
        return manager.add_completion_subscribers(run_id, subscribers)

    if strict:
        if db is None:
            raise SubscriptionPersistenceError(
                f"Cannot persist completion subscribers for run {run_id}: database unavailable"
            )
        try:
            inserted_session_ids = local_persist(db)
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
                inserted_session_ids = local_persist(db)
            except (ImportError, psycopg.DatabaseError):
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


__all__ = [
    "remove_agent_completion_subscribers",
    "subscribe_agent_completion",
]
