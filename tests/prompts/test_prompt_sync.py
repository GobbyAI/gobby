"""Tests for bundled prompt synchronization."""

import pytest

from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.prompts import LocalPromptManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Use the migrated PostgreSQL hub database fixture."""
    return temp_db


class TestSyncBundledPrompts:
    """Tests for sync_bundled_prompts()."""

    def test_sync_creates_records(self, db) -> None:
        """Test that sync creates prompt records from bundled .md files."""
        result = sync_bundled_prompts(db)

        assert result["synced"] > 0
        assert len(result["errors"]) == 0

        # Verify records exist in DB
        manager = LocalPromptManager(db)
        records = manager.list_prompts(scope="bundled")
        assert len(records) > 0

    def test_sync_idempotent(self, db) -> None:
        """Test that running sync twice doesn't create duplicates."""
        result1 = sync_bundled_prompts(db)
        result2 = sync_bundled_prompts(db)

        # Second run should skip all (no changes)
        assert result2["synced"] == 0
        assert result2["skipped"] == result1["synced"]

        # Total count should be same
        manager = LocalPromptManager(db)
        assert manager.count_prompts(scope="bundled") == result1["synced"]

    def test_sync_detects_updates(self, db) -> None:
        """Test that sync updates changed content."""
        # First sync
        sync_bundled_prompts(db)

        # Manually modify a bundled record
        manager = LocalPromptManager(db, dev_mode=True)
        records = manager.list_prompts(scope="bundled", limit=1)
        assert len(records) > 0
        record = records[0]
        manager.update_prompt(record.id, content="Modified content")

        # Second sync should detect the change and update
        result = sync_bundled_prompts(db)
        assert result["updated"] > 0

    def test_sync_sets_scope_bundled(self, db) -> None:
        """Test that all synced records have scope='bundled'."""
        sync_bundled_prompts(db)

        manager = LocalPromptManager(db)
        records = manager.list_prompts()
        for record in records:
            assert record.scope == "bundled"

    def test_known_templates_synced(self, db) -> None:
        """Test that known bundled templates are synced."""
        sync_bundled_prompts(db)

        manager = LocalPromptManager(db)

        # These templates should exist in the bundled prompts
        known_templates = [
            "expansion/system",
            "expansion/user",
            "validation/validate",
        ]

        for name in known_templates:
            record = manager.get_by_name(name)
            assert record is not None, f"Expected bundled template '{name}' not found"
            assert record.content != ""
