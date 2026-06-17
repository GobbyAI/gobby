"""Tests that obsolete memory media storage support is removed."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager, Memory

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    """Create a test database with migrations applied."""
    yield temp_db


@pytest.fixture
def memory_manager(db: HubDatabase) -> LocalMemoryManager:
    """Create a memory manager with test database."""
    return LocalMemoryManager(db)


class TestMemoryMediaFieldRemoved:
    """Tests for removed Memory dataclass media field."""

    def test_memory_has_no_media_field(self) -> None:
        assert "media" not in Memory.__dataclass_fields__

    def test_memory_to_dict_excludes_media(self) -> None:
        memory = Memory(
            id="mm-test",
            memory_type="fact",
            content="Test memory",
            created_at="2026-01-19T00:00:00Z",
            updated_at="2026-01-19T00:00:00Z",
        )

        assert "media" not in memory.to_dict()


class TestLocalMemoryManagerMediaRemoved:
    """Tests for removed LocalMemoryManager media parameters."""

    def test_create_and_update_signatures_do_not_accept_media(self) -> None:
        create_params = inspect.signature(LocalMemoryManager.create_memory).parameters
        update_params = inspect.signature(LocalMemoryManager.update_memory).parameters

        assert "media" not in create_params
        assert "media" not in update_params

    def test_create_memory_returns_memory_without_media(
        self, memory_manager: LocalMemoryManager
    ) -> None:
        memory = memory_manager.create_memory(
            content="Plain text memory",
            memory_type="fact",
        )

        assert not hasattr(memory, "media")


class TestMediaColumnRemoved:
    """Tests for removed memories.media database column."""

    def test_memories_table_has_no_media_column(self, db: HubDatabase) -> None:
        columns = {
            row["column_name"]
            for row in db.fetchall(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                ("memories",),
            )
        }

        assert "media" not in columns

    def test_postgres_baseline_omits_media_column(self) -> None:
        baseline = (REPO_ROOT / "src/gobby/storage/postgres_baseline_schema.sql").read_text(
            encoding="utf-8"
        )

        assert "media JSONB" not in baseline
