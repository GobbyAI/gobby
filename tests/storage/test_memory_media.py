"""TDD tests for Memory media column support.

Tests for Phase 2: Multimodal Support - adding media attachments to memories.

RED phase: These tests define expected behavior before implementation.
"""

import json

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager, Memory

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase):
    """Create a test database with migrations applied."""
    database = temp_db
    yield database


@pytest.fixture
def memory_manager(db):
    """Create a memory manager for testing."""
    return LocalMemoryManager(db)


# =============================================================================
# Test: Memory dataclass has media field
# =============================================================================


class TestMemoryMediaField:
    """Tests for Memory dataclass media field."""

    def test_memory_has_media_field(self) -> None:
        """Test that Memory dataclass has an optional media field."""
        memory = Memory(
            id="mm-test",
            memory_type="fact",
            content="Test memory",
            created_at="2026-01-19T00:00:00Z",
            updated_at="2026-01-19T00:00:00Z",
        )
        # Media field should exist and default to None
        assert hasattr(memory, "media")
        assert memory.media is None

    def test_memory_with_media_value(self) -> None:
        """Test that Memory can be created with media value."""
        media_data = json.dumps(
            {
                "path": "/path/to/image.png",
                "mime_type": "image/png",
                "description": "Screenshot of error",
            }
        )
        memory = Memory(
            id="mm-test",
            memory_type="fact",
            content="Test memory with image",
            created_at="2026-01-19T00:00:00Z",
            updated_at="2026-01-19T00:00:00Z",
            media=media_data,
        )
        assert memory.media == media_data

    def test_memory_to_dict_includes_media(self) -> None:
        """Test that Memory.to_dict() includes media field."""
        media_data = json.dumps({"path": "/img.png", "mime_type": "image/png"})
        memory = Memory(
            id="mm-test",
            memory_type="fact",
            content="Test",
            created_at="2026-01-19T00:00:00Z",
            updated_at="2026-01-19T00:00:00Z",
            media=media_data,
        )
        result = memory.to_dict()
        assert "media" in result
        assert result["media"] == media_data

    def test_memory_to_dict_media_none(self) -> None:
        """Test that Memory.to_dict() includes media as None when not set."""
        memory = Memory(
            id="mm-test",
            memory_type="fact",
            content="Test",
            created_at="2026-01-19T00:00:00Z",
            updated_at="2026-01-19T00:00:00Z",
        )
        result = memory.to_dict()
        assert "media" in result
        assert result["media"] is None


# =============================================================================
# Test: Database schema has media column
# =============================================================================


class TestMediaColumnMigration:
    """Tests for media column in database schema."""

    def test_memories_table_has_media_column(self, db) -> None:
        """Test that memories table has a media column after migration."""
        columns = {
            row["column_name"]
            for row in db.fetchall(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                ("memories",),
            )
        }
        assert "media" in columns

    def test_media_column_allows_null(self, db) -> None:
        """Test that media column allows NULL values."""
        # Insert a memory without media
        db.execute("""
            INSERT INTO memories (id, memory_type, content, created_at, updated_at)
            VALUES ('mm-test', 'fact', 'Test', '2026-01-19', '2026-01-19')
        """)
        # Should not raise - media defaults to NULL
        cursor = db.execute("SELECT media FROM memories WHERE id = 'mm-test'")
        row = cursor.fetchone()
        assert row["media"] is None


# =============================================================================
# Test: Memory.from_row handles media column
# =============================================================================


class TestMemoryFromRowMedia:
    """Tests for Memory.from_row() handling media column."""

    def test_from_row_with_media(self, db) -> None:
        """Test that Memory.from_row() correctly reads media from database row."""
        media_data = json.dumps(
            {
                "path": "/path/to/image.png",
                "mime_type": "image/png",
                "description": "Test image",
            }
        )
        db.execute(
            """
            INSERT INTO memories (id, memory_type, content, created_at, updated_at,
                                  access_count, tags, media)
            VALUES (%s, 'fact', 'Test content', '2026-01-19', '2026-01-19',
                    0, '[]', %s)
        """,
            ("mm-test", media_data),
        )

        cursor = db.execute("SELECT * FROM memories WHERE id = 'mm-test'")
        row = cursor.fetchone()

        memory = Memory.from_row(row)
        assert json.loads(memory.media) == json.loads(media_data)

    def test_from_row_without_media(self, db) -> None:
        """Test that Memory.from_row() handles NULL media gracefully."""
        db.execute("""
            INSERT INTO memories (id, memory_type, content, created_at, updated_at,
                                  access_count, tags)
            VALUES ('mm-test', 'fact', 'Test', '2026-01-19', '2026-01-19',
                    0, '[]')
        """)

        cursor = db.execute("SELECT * FROM memories WHERE id = 'mm-test'")
        row = cursor.fetchone()

        memory = Memory.from_row(row)
        assert memory.media is None


# =============================================================================
# Test: LocalMemoryManager CRUD with media
# =============================================================================


class TestMemoryManagerMedia:
    """Tests for LocalMemoryManager media support."""

    def test_create_memory_with_media(self, memory_manager) -> None:
        """Test creating a memory with media attachment."""
        media_data = json.dumps(
            {
                "path": "/screenshots/error.png",
                "mime_type": "image/png",
                "description": "Error screenshot",
            }
        )
        memory = memory_manager.create_memory(
            content="Found an error in the login flow",
            memory_type="fact",
            media=media_data,
        )
        assert json.loads(memory.media) == json.loads(media_data)

    def test_create_memory_without_media(self, memory_manager) -> None:
        """Test creating a memory without media (default behavior)."""
        memory = memory_manager.create_memory(
            content="Simple text memory",
            memory_type="fact",
        )
        assert memory.media is None

    def test_get_memory_returns_media(self, memory_manager) -> None:
        """Test that get_memory returns the media field."""
        media_data = json.dumps({"path": "/img.png"})
        created = memory_manager.create_memory(
            content="Memory with image",
            media=media_data,
        )
        retrieved = memory_manager.get_memory(created.id)
        assert retrieved.media is not None
        assert json.loads(retrieved.media) == json.loads(media_data)

    def test_update_memory_media(self, memory_manager) -> None:
        """Test updating a memory's media field."""
        # Create without media
        created = memory_manager.create_memory(content="No media yet")
        assert created.media is None

        # Update to add media
        media_data = json.dumps({"path": "/new-image.png"})
        updated = memory_manager.update_memory(created.id, media=media_data)
        assert updated.media is not None
        assert json.loads(updated.media) == json.loads(media_data)

    def test_update_memory_remove_media(self, memory_manager) -> None:
        """Test removing media from a memory."""
        media_data = json.dumps({"path": "/img.png"})
        created = memory_manager.create_memory(
            content="Has media",
            media=media_data,
        )
        assert created.media is not None

        # Update to remove media (set to None)
        # Note: Implementation may need to handle explicit None vs not-provided
        updated = memory_manager.update_memory(created.id, media=None)
        assert updated.media is None

    def test_list_memories_includes_media(self, memory_manager) -> None:
        """Test that list_memories returns memories with media field."""
        media_data = json.dumps({"path": "/img.png"})
        memory_manager.create_memory(content="With media", media=media_data)
        memory_manager.create_memory(content="Without media")

        memories = memory_manager.list_memories()
        assert len(memories) >= 2

        # Find the memory with media
        with_media = next((m for m in memories if m.content == "With media"), None)
        assert with_media is not None
        assert with_media.media is not None
        assert json.loads(with_media.media) == json.loads(media_data)

        # Find the memory without media
        without_media = next((m for m in memories if m.content == "Without media"), None)
        assert without_media is not None
        assert without_media.media is None
