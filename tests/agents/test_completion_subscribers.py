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
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import (
    CompletionSubscriberManager,
    PipelineSubscriberStorageError,
)
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


def _create_agent_run(
    db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, object],
    *,
    external_id: str,
    status: str = "pending",
) -> tuple[str, str, LocalAgentRunManager]:
    run_manager = LocalAgentRunManager(db)
    with pytest.MonkeyPatch.context() as identity_patch:
        patch_local_machine_id(identity_patch, LOCAL_MACHINE_ID)
        session = session_manager.register(
            external_id=external_id,
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=str(sample_project["id"]),
        )
        run = run_manager.create(
            parent_session_id=session.id,
            provider="claude",
            prompt="test active agent wait",
        )
    if status == "running":
        started = run_manager.start(run.id)
        assert started is not None
    return session.id, run.id, run_manager


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


@pytest.mark.parametrize("status", ["pending", "running"])
def test_has_active_agent_wait_matches_durable_subscription_and_owned_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, object],
    status: str,
) -> None:
    session_id, run_id, _run_manager = _create_agent_run(
        temp_db,
        session_manager,
        sample_project,
        external_id=f"active-agent-wait-{status}",
        status=status,
    )
    subscriber_manager = CompletionSubscriberManager(temp_db)
    subscriber_manager.add_completion_subscriber(run_id, session_id)

    assert subscriber_manager.has_active_agent_wait(session_id) is True


def test_has_active_agent_wait_translates_storage_failures() -> None:
    db = MagicMock(spec=HubDatabase)
    db.fetchone.side_effect = RuntimeError("database unavailable")
    subscriber_manager = CompletionSubscriberManager(db)

    with pytest.raises(PipelineSubscriberStorageError, match="session-id"):
        subscriber_manager.has_active_agent_wait("session-id")


def test_has_active_agent_wait_rejects_missing_foreign_and_orphan_subscriptions(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, object],
) -> None:
    owner_session_id, run_id, _run_manager = _create_agent_run(
        temp_db,
        session_manager,
        sample_project,
        external_id="active-agent-wait-owner",
    )
    with pytest.MonkeyPatch.context() as identity_patch:
        patch_local_machine_id(identity_patch, LOCAL_MACHINE_ID)
        foreign_session = session_manager.register(
            external_id="active-agent-wait-foreign",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=str(sample_project["id"]),
        )
    subscriber_manager = CompletionSubscriberManager(temp_db)

    assert subscriber_manager.has_active_agent_wait(owner_session_id) is False

    subscriber_manager.add_completion_subscriber(run_id, foreign_session.id)
    assert subscriber_manager.has_active_agent_wait(foreign_session.id) is False

    orphan_run_id = "55361235-ff5f-5de3-88f4-c98c82f7f0c3"
    subscriber_manager.add_completion_subscriber(orphan_run_id, owner_session_id)
    assert subscriber_manager.has_active_agent_wait(owner_session_id) is False


def test_terminal_agent_run_automatically_rearms_wait_state(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, object],
) -> None:
    session_id, run_id, run_manager = _create_agent_run(
        temp_db,
        session_manager,
        sample_project,
        external_id="terminal-agent-wait",
    )
    subscriber_manager = CompletionSubscriberManager(temp_db)
    subscriber_manager.add_completion_subscriber(run_id, session_id)
    assert subscriber_manager.has_active_agent_wait(session_id) is True

    completed = run_manager.complete(run_id, result="done")
    assert completed is not None
    assert subscriber_manager.get_completion_subscribers(run_id) == [session_id]
    assert subscriber_manager.has_active_agent_wait(session_id) is False
