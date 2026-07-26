"""Tests for agent completion subscriber helpers."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.agents.completion_subscribers import (
    SubscriptionPersistenceError,
    remove_agent_completion_subscribers,
    subscribe_agent_completion,
)
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager

pytestmark = pytest.mark.unit


def test_subscribe_agent_completion_has_no_session_manager_parameter() -> None:
    parameters = inspect.signature(subscribe_agent_completion).parameters

    assert "session_manager" not in parameters


def test_subscribe_agent_completion_registers_only_requested_child() -> None:
    completion_registry = CompletionEventRegistry()

    result = subscribe_agent_completion(
        completion_registry=completion_registry,
        run_id="run-1",
        subscriber_session_id="child-session",
    )

    assert result.subscribers == ["child-session"]
    assert completion_registry.get_subscribers("run-1") == ["child-session"]


def test_subscribe_agent_completion_does_not_swallow_subscriber_manager_errors() -> None:
    completion_registry = CompletionEventRegistry()

    with patch(
        "gobby.storage.pipeline_subscribers.CompletionSubscriberManager",
        side_effect=ValueError("bad subscriber store"),
    ):
        with pytest.raises(ValueError, match="bad subscriber store"):
            subscribe_agent_completion(
                completion_registry=completion_registry,
                run_id="run-1",
                subscriber_session_id="child-session",
                db=MagicMock(),
            )

    assert completion_registry.get_subscribers("run-1") == ["child-session"]


def test_subscribe_agent_completion_intentional_subscriptions_merge_in_memory_and_storage(
    temp_db: HubDatabase,
) -> None:
    run_id = "55361235-ff5f-5de3-88f4-c98c82f7f0c3"
    root_session_id = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
    child_session_id = "7a378a57-18dd-56d9-be74-0fcb8a19376d"
    completion_registry = CompletionEventRegistry()

    root_subscription = subscribe_agent_completion(
        completion_registry=completion_registry,
        run_id=run_id,
        subscriber_session_id=root_session_id,
        db=temp_db,
    )
    child_subscription = subscribe_agent_completion(
        completion_registry=completion_registry,
        run_id=run_id,
        subscriber_session_id=child_session_id,
        db=temp_db,
    )
    repeated_child_subscription = subscribe_agent_completion(
        completion_registry=completion_registry,
        run_id=run_id,
        subscriber_session_id=child_session_id,
        db=temp_db,
    )

    expected_subscribers = [root_session_id, child_session_id]
    durable_subscribers = CompletionSubscriberManager(temp_db).get_completion_subscribers(run_id)
    assert root_subscription.subscribers == [root_session_id]
    assert root_subscription.created_fresh_entry is True
    assert root_subscription.inserted_session_ids == [root_session_id]
    assert child_subscription.subscribers == [child_session_id]
    assert child_subscription.created_fresh_entry is False
    assert child_subscription.inserted_session_ids == [child_session_id]
    assert repeated_child_subscription.subscribers == [child_session_id]
    assert repeated_child_subscription.created_fresh_entry is False
    assert repeated_child_subscription.inserted_session_ids == []
    assert completion_registry.get_subscribers(run_id) == expected_subscribers
    assert set(durable_subscribers) == set(expected_subscribers)
    assert len(durable_subscribers) == len(expected_subscribers)


def test_subscribe_agent_completion_default_persistence_failure_is_best_effort() -> None:
    completion_registry = CompletionEventRegistry()
    manager = MagicMock()
    manager.add_completion_subscribers.side_effect = psycopg.DatabaseError("insert failed")

    with patch(
        "gobby.storage.pipeline_subscribers.CompletionSubscriberManager",
        return_value=manager,
    ):
        result = subscribe_agent_completion(
            completion_registry=completion_registry,
            run_id="run-1",
            subscriber_session_id="child-session",
            db=MagicMock(),
        )

    assert result.subscribers == ["child-session"]
    assert result.created_fresh_entry is True
    assert result.inserted_session_ids == []
    assert completion_registry.get_subscribers("run-1") == ["child-session"]


def test_subscribe_agent_completion_surfaces_inserted_session_ids() -> None:
    manager = MagicMock()
    manager.add_completion_subscribers.return_value = ["child-session"]

    with patch(
        "gobby.storage.pipeline_subscribers.CompletionSubscriberManager",
        return_value=manager,
    ):
        result = subscribe_agent_completion(
            completion_registry=CompletionEventRegistry(),
            run_id="run-1",
            subscriber_session_id="child-session",
            db=MagicMock(),
        )

    assert result.inserted_session_ids == ["child-session"]


def test_subscribe_agent_completion_strict_persistence_failure_does_not_register() -> None:
    completion_registry = CompletionEventRegistry()
    manager = MagicMock()
    manager.add_completion_subscribers.side_effect = psycopg.DatabaseError("insert failed")

    with (
        patch(
            "gobby.storage.pipeline_subscribers.CompletionSubscriberManager",
            return_value=manager,
        ),
        pytest.raises(SubscriptionPersistenceError, match="run-1"),
    ):
        subscribe_agent_completion(
            completion_registry=completion_registry,
            run_id="run-1",
            subscriber_session_id="child-session",
            db=MagicMock(),
            strict=True,
        )

    assert completion_registry.is_registered("run-1") is False


def test_remove_agent_completion_subscribers_removes_only_selected_sessions(
    temp_db: HubDatabase,
) -> None:
    run_id = "55361235-ff5f-5de3-88f4-c98c82f7f0c3"
    retained_session_id = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
    removed_session_id = "7a378a57-18dd-56d9-be74-0fcb8a19376d"
    manager = CompletionSubscriberManager(db=temp_db)
    manager.add_completion_subscribers(
        run_id,
        [retained_session_id, removed_session_id],
    )

    remove_agent_completion_subscribers(
        db=temp_db,
        run_id=run_id,
        session_ids=[removed_session_id],
    )

    assert manager.get_completion_subscribers(run_id) == [retained_session_id]
