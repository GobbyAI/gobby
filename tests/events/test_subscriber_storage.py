"""Tests for completion subscriber DB persistence."""

from __future__ import annotations

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Use the migrated PostgreSQL hub database fixture."""
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> LocalPipelineExecutionManager:
    return LocalPipelineExecutionManager(db=db, project_id="11111111-1111-4111-8111-111111110001")


class TestCompletionSubscribers:
    """CRUD for completion_subscribers table."""

    def test_add_subscriber(self, manager: LocalPipelineExecutionManager) -> None:
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "9264a39c-68db-5eed-917c-6f7babb8e6b1"
        )
        subs = manager.get_completion_subscribers("55361235-ff5f-5de3-88f4-c98c82f7f0c3")
        assert subs == ["9264a39c-68db-5eed-917c-6f7babb8e6b1"]

    def test_add_multiple_subscribers(self, manager: LocalPipelineExecutionManager) -> None:
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "9264a39c-68db-5eed-917c-6f7babb8e6b1"
        )
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "7a378a57-18dd-56d9-be74-0fcb8a19376d"
        )
        subs = manager.get_completion_subscribers("55361235-ff5f-5de3-88f4-c98c82f7f0c3")
        assert set(subs) == {
            "9264a39c-68db-5eed-917c-6f7babb8e6b1",
            "7a378a57-18dd-56d9-be74-0fcb8a19376d",
        }

    def test_add_subscriber_idempotent(self, manager: LocalPipelineExecutionManager) -> None:
        """Adding same subscriber twice doesn't duplicate."""
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "9264a39c-68db-5eed-917c-6f7babb8e6b1"
        )
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "9264a39c-68db-5eed-917c-6f7babb8e6b1"
        )
        subs = manager.get_completion_subscribers("55361235-ff5f-5de3-88f4-c98c82f7f0c3")
        assert subs == ["9264a39c-68db-5eed-917c-6f7babb8e6b1"]

    def test_get_subscribers_empty(self, manager: LocalPipelineExecutionManager) -> None:
        subs = manager.get_completion_subscribers("00000000-0000-0000-0000-0000000000ff")
        assert subs == []

    def test_remove_subscribers(self, manager: LocalPipelineExecutionManager) -> None:
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "9264a39c-68db-5eed-917c-6f7babb8e6b1"
        )
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "7a378a57-18dd-56d9-be74-0fcb8a19376d"
        )
        manager.remove_completion_subscribers("55361235-ff5f-5de3-88f4-c98c82f7f0c3")
        subs = manager.get_completion_subscribers("55361235-ff5f-5de3-88f4-c98c82f7f0c3")
        assert subs == []

    def test_remove_selected_subscribers(self, manager: LocalPipelineExecutionManager) -> None:
        completion_id = "55361235-ff5f-5de3-88f4-c98c82f7f0c3"
        retained_session_id = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
        removed_session_id = "7a378a57-18dd-56d9-be74-0fcb8a19376d"
        manager.add_completion_subscribers(
            completion_id,
            [retained_session_id, removed_session_id],
        )

        manager.remove_completion_subscribers(
            completion_id,
            session_ids=[removed_session_id],
        )

        assert manager.get_completion_subscribers(completion_id) == [retained_session_id]

    def test_remove_subscribers_noop_if_none(self, manager: LocalPipelineExecutionManager) -> None:
        """Remove on nonexistent completion_id doesn't raise."""
        result = manager.remove_completion_subscribers("00000000-0000-0000-0000-0000000000ff")
        assert result is None
        assert manager.get_completion_subscribers("00000000-0000-0000-0000-0000000000ff") == []

    def test_subscribers_isolated_by_completion_id(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        manager.add_completion_subscriber(
            "796ce97e-38ee-508a-bdc0-f3ce2dded342", "12313230-63a9-5fd2-bdbb-f793325d2c16"
        )
        manager.add_completion_subscriber(
            "c7246830-9f72-5c2c-9a9c-bc004a24a0a3", "e3c98b06-11a5-5e52-9b82-b47a220be090"
        )
        assert manager.get_completion_subscribers("796ce97e-38ee-508a-bdc0-f3ce2dded342") == [
            "12313230-63a9-5fd2-bdbb-f793325d2c16"
        ]
        assert manager.get_completion_subscribers("c7246830-9f72-5c2c-9a9c-bc004a24a0a3") == [
            "e3c98b06-11a5-5e52-9b82-b47a220be090"
        ]

    def test_add_completion_subscribers_bulk(self, manager: LocalPipelineExecutionManager) -> None:
        """Bulk add multiple subscribers at once."""
        inserted = manager.add_completion_subscribers(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3",
            [
                "9264a39c-68db-5eed-917c-6f7babb8e6b1",
                "7a378a57-18dd-56d9-be74-0fcb8a19376d",
                "204df9de-a672-51b8-811a-0fc1a71bca39",
            ],
        )
        assert inserted == [
            "9264a39c-68db-5eed-917c-6f7babb8e6b1",
            "7a378a57-18dd-56d9-be74-0fcb8a19376d",
            "204df9de-a672-51b8-811a-0fc1a71bca39",
        ]
        subs = manager.get_completion_subscribers("55361235-ff5f-5de3-88f4-c98c82f7f0c3")
        assert set(subs) == {
            "9264a39c-68db-5eed-917c-6f7babb8e6b1",
            "7a378a57-18dd-56d9-be74-0fcb8a19376d",
            "204df9de-a672-51b8-811a-0fc1a71bca39",
        }

        inserted = manager.add_completion_subscribers(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3",
            [
                "204df9de-a672-51b8-811a-0fc1a71bca39",
                "ba8e8e0d-c7dd-5b7a-88d4-863678433d34",
            ],
        )
        assert inserted == ["ba8e8e0d-c7dd-5b7a-88d4-863678433d34"]

    def test_remove_completion_subscribers_for_terminal_agent_runs(
        self,
        manager: LocalPipelineExecutionManager,
        db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Startup sweep removes only subscriber rows tied to terminal agent runs."""
        session = session_manager.register(
            external_id="terminal-sweep-session",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
        )
        run_manager = LocalAgentRunManager(db)
        terminal_run = run_manager.create(
            parent_session_id=session.id,
            provider="codex",
            prompt="done",
        )
        active_run = run_manager.create(
            parent_session_id=session.id,
            provider="codex",
            prompt="still active",
        )
        assert run_manager.complete(terminal_run.id, result="done") is not None

        manager.add_completion_subscriber(terminal_run.id, "f9b308d7-dd35-5288-a349-17cd375762df")
        manager.add_completion_subscriber(active_run.id, "a4497f8f-83d6-5db4-8f2a-fbcfef9d4985")
        manager.add_completion_subscriber(
            "55361235-ff5f-5de3-88f4-c98c82f7f0c3", "61768209-00e7-5191-9737-e6f193ea71e9"
        )

        removed = manager.remove_completion_subscribers_for_terminal_agent_runs()

        assert removed == 1
        assert manager.get_completion_subscribers(terminal_run.id) == []
        assert manager.get_completion_subscribers(active_run.id) == [
            "a4497f8f-83d6-5db4-8f2a-fbcfef9d4985"
        ]
        assert manager.get_completion_subscribers("55361235-ff5f-5de3-88f4-c98c82f7f0c3") == [
            "61768209-00e7-5191-9737-e6f193ea71e9"
        ]
