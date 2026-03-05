"""Tests for completion subscriber DB persistence."""

from __future__ import annotations

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    """Create a fresh in-memory database with migrations applied."""
    db_path = tmp_path / "test.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalPipelineExecutionManager:
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
        manager.remove_completion_subscribers("nonexistent")

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
