"""Tests for MemoryManager method renames (remember→create_memory, etc.)."""

from __future__ import annotations

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager

pytestmark = pytest.mark.unit

# Valid-format UUIDs: projects.id and memories.id are native uuid columns.
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
MISSING_MEMORY_ID = "99999999-9999-4999-8999-999999999999"


@pytest.fixture
def db(hub_db):
    """Create a temporary hub database for testing."""
    return hub_db


@pytest.fixture
def manager(db):
    """Create a MemoryManager with default config."""
    config = MemoryConfig(enabled=True, backend="local")
    return MemoryManager(db=db, config=config)


@pytest.mark.asyncio
async def test_create_memory_exists(manager: MemoryManager) -> None:
    """create_memory() should exist and create a memory."""
    memory = await manager.create_memory(
        content="test fact",
        memory_type="fact",
    )
    assert memory.content == "test fact"


@pytest.mark.asyncio
async def test_search_memories_exists(manager: MemoryManager) -> None:
    """search_memories() should exist and be callable."""
    results = await manager.search_memories(query=None, project_id=PROJECT_ID, limit=5)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_delete_memory_exists(manager: MemoryManager) -> None:
    """delete_memory() should exist and return a bool."""
    result = await manager.delete_memory(MISSING_MEMORY_ID)
    assert result is False


def test_old_remember_removed(manager: MemoryManager) -> None:
    """remember() should no longer exist."""
    assert not hasattr(manager, "remember")


def test_old_recall_removed(manager: MemoryManager) -> None:
    """recall() should no longer exist."""
    assert not hasattr(manager, "recall")


def test_old_recall_as_context_removed(manager: MemoryManager) -> None:
    """recall_as_context() should no longer exist."""
    assert not hasattr(manager, "recall_as_context")


def test_old_forget_removed(manager: MemoryManager) -> None:
    """forget() should no longer exist."""
    assert not hasattr(manager, "forget")


def test_update_memory_still_exists(manager: MemoryManager) -> None:
    """update_memory() should still exist (name unchanged)."""
    assert hasattr(manager, "update_memory")
