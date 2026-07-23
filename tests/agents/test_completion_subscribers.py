"""Tests for agent completion subscriber helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.agents.completion_subscribers import (
    SubscriptionPersistenceError,
    completion_subscriber_lineage,
    remove_agent_completion_subscribers,
    subscribe_agent_completion,
)
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager

pytestmark = pytest.mark.unit


def test_completion_subscriber_lineage_falls_back_on_unexpected_error() -> None:
    session_manager = MagicMock()
    session_manager.get.side_effect = RuntimeError("lineage failed")

    subscribers = completion_subscriber_lineage("child-session", session_manager)

    assert subscribers == ["child-session"]


def test_completion_subscriber_lineage_includes_parent_chain() -> None:
    root = SimpleNamespace(id="root-session", parent_session_id=None)
    child = SimpleNamespace(id="child-session", parent_session_id="root-session")
    session_manager = MagicMock()
    session_manager.get.side_effect = lambda session_id: {
        "root-session": root,
        "child-session": child,
    }[session_id]

    subscribers = completion_subscriber_lineage("child-session", session_manager)

    assert subscribers == ["root-session", "child-session"]


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


def test_subscribe_agent_completion_reregistration_preserves_subscribers() -> None:
    completion_registry = CompletionEventRegistry()
    session_manager = MagicMock()
    session_manager.get.side_effect = [
        SimpleNamespace(id="child-1", parent_session_id="parent-1"),
        SimpleNamespace(id="parent-1", parent_session_id=None),
        SimpleNamespace(id="child-2", parent_session_id="parent-2"),
        SimpleNamespace(id="parent-2", parent_session_id=None),
    ]

    first_subscribers = subscribe_agent_completion(
        completion_registry=completion_registry,
        run_id="run-1",
        subscriber_session_id="child-1",
        session_manager=session_manager,
    )
    second_subscribers = subscribe_agent_completion(
        completion_registry=completion_registry,
        run_id="run-1",
        subscriber_session_id="child-2",
        session_manager=session_manager,
    )

    assert first_subscribers.subscribers == ["parent-1", "child-1"]
    assert first_subscribers.created_fresh_entry is True
    assert first_subscribers.inserted_session_ids == []
    assert second_subscribers.subscribers == ["parent-2", "child-2"]
    assert second_subscribers.created_fresh_entry is False
    assert second_subscribers.inserted_session_ids == []
    assert completion_registry.get_subscribers("run-1") == [
        "parent-1",
        "child-1",
        "parent-2",
        "child-2",
    ]


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
