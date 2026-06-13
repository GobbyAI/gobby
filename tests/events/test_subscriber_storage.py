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
    return LocalPipelineExecutionManager(db=db, project_id="test-project")


class TestCompletionSubscribers:
    """CRUD for completion_subscribers table."""

    def test_add_subscriber(self, manager: LocalPipelineExecutionManager) -> None:
        manager.add_completion_subscriber("pe-abc123", "sess-1")
        subs = manager.get_completion_subscribers("pe-abc123")
        assert subs == ["sess-1"]

    def test_add_multiple_subscribers(self, manager: LocalPipelineExecutionManager) -> None:
        manager.add_completion_subscriber("pe-abc123", "sess-1")
        manager.add_completion_subscriber("pe-abc123", "sess-2")
        subs = manager.get_completion_subscribers("pe-abc123")
        assert set(subs) == {"sess-1", "sess-2"}

    def test_add_subscriber_idempotent(self, manager: LocalPipelineExecutionManager) -> None:
        """Adding same subscriber twice doesn't duplicate."""
        manager.add_completion_subscriber("pe-abc123", "sess-1")
        manager.add_completion_subscriber("pe-abc123", "sess-1")
        subs = manager.get_completion_subscribers("pe-abc123")
        assert subs == ["sess-1"]

    def test_get_subscribers_empty(self, manager: LocalPipelineExecutionManager) -> None:
        subs = manager.get_completion_subscribers("nonexistent")
        assert subs == []

    def test_remove_subscribers(self, manager: LocalPipelineExecutionManager) -> None:
        manager.add_completion_subscriber("pe-abc123", "sess-1")
        manager.add_completion_subscriber("pe-abc123", "sess-2")
        manager.remove_completion_subscribers("pe-abc123")
        subs = manager.get_completion_subscribers("pe-abc123")
        assert subs == []

    def test_remove_subscribers_noop_if_none(self, manager: LocalPipelineExecutionManager) -> None:
        """Remove on nonexistent completion_id doesn't raise."""
        result = manager.remove_completion_subscribers("nonexistent")
        assert result is None
        assert manager.get_completion_subscribers("nonexistent") == []

    def test_subscribers_isolated_by_completion_id(
        self, manager: LocalPipelineExecutionManager
    ) -> None:
        manager.add_completion_subscriber("pe-1", "sess-a")
        manager.add_completion_subscriber("pe-2", "sess-b")
        assert manager.get_completion_subscribers("pe-1") == ["sess-a"]
        assert manager.get_completion_subscribers("pe-2") == ["sess-b"]

    def test_add_completion_subscribers_bulk(self, manager: LocalPipelineExecutionManager) -> None:
        """Bulk add multiple subscribers at once."""
        manager.add_completion_subscribers("pe-abc123", ["sess-1", "sess-2", "sess-3"])
        subs = manager.get_completion_subscribers("pe-abc123")
        assert set(subs) == {"sess-1", "sess-2", "sess-3"}

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

        manager.add_completion_subscriber(terminal_run.id, "sess-terminal")
        manager.add_completion_subscriber(active_run.id, "sess-active")
        manager.add_completion_subscriber("pe-abc123", "sess-pipeline")

        removed = manager.remove_completion_subscribers_for_terminal_agent_runs()

        assert removed == 1
        assert manager.get_completion_subscribers(terminal_run.id) == []
        assert manager.get_completion_subscribers(active_run.id) == ["sess-active"]
        assert manager.get_completion_subscribers("pe-abc123") == ["sess-pipeline"]
