import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.persistence import MemoryBackupConfig
from gobby.storage.memories import Memory
from gobby.sync.memories import MemorySyncManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_memory_manager():
    mm = MagicMock()
    mm.list_memories = MagicMock(
        return_value=[
            Memory(
                id="m1",
                content="test memory",
                memory_type="fact",
                created_at="2023-01-01T00:00:00Z",
                updated_at="2023-01-01T00:00:00Z",
                access_count=0,
                last_accessed_at=None,
                tags=[],
                project_id="p1",
                source_type="user",
                source_session_id=None,
            )
        ]
    )
    mm.remember = MagicMock()
    mm.content_exists = MagicMock(return_value=False)  # For dedup checks during import
    # storage.create_memory is used for sync import (skips auto-embedding)
    mm.storage = MagicMock()
    mm.storage.create_memory = MagicMock()
    return mm


@pytest.fixture
def sync_config():
    return MemoryBackupConfig(enabled=True, export_debounce=0.1)


@pytest.fixture
def sync_manager(mock_db, mock_memory_manager, sync_config):
    return MemorySyncManager(mock_db, mock_memory_manager, sync_config)


@pytest.mark.asyncio
async def test_get_export_path_absolute(sync_manager, tmp_path):
    """Test that absolute paths are returned as-is."""
    sync_manager.export_path = tmp_path / "memories.jsonl"
    path = sync_manager._get_export_path()
    assert path == tmp_path / "memories.jsonl"


@pytest.mark.asyncio
async def test_get_export_path_relative_with_project(sync_manager):
    """Test that relative paths resolve against project context."""
    sync_manager.export_path = Path(".gobby/memories.jsonl")
    mock_context = {"project_path": "/tmp/project"}
    with patch("gobby.utils.project_context.get_project_context", return_value=mock_context):
        path = sync_manager._get_export_path()
        # Compare resolved paths (handles macOS /tmp -> /private/tmp symlink)
        expected = (Path("/tmp/project") / ".gobby" / "memories.jsonl").resolve()
        assert path == expected


@pytest.mark.asyncio
async def test_export_to_files(sync_manager, tmp_path):
    # Override export_path to use tmp_path
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    count = await sync_manager.export_to_files()

    # Check memories.jsonl
    assert mem_file.exists()
    lines = mem_file.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["content"] == "test memory"
    assert count == 1


@pytest.mark.asyncio
async def test_import_from_files(sync_manager, tmp_path):
    """Test importing memories from JSONL file."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    # Create dummy memory file
    mem_file.write_text(json.dumps({"content": "imported memory", "type": "fact"}) + "\n")

    count = await sync_manager.import_from_files()

    # Verify storage.create_memory called (sync import bypasses auto-embedding)
    sync_manager.memory_manager.storage.create_memory.assert_called()
    call_args = sync_manager.memory_manager.storage.create_memory.call_args[1]
    assert call_args["content"] == "imported memory"
    assert call_args["memory_type"] == "fact"
    assert count == 1


@pytest.mark.asyncio
async def test_import_skips_ephemeral_implementation_notes(sync_manager, tmp_path):
    """Run-specific implementation notes are not imported from JSONL."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file
    mem_file.write_text(
        json.dumps(
            {
                "content": "Gobby build #epic E2E docs test #14353 completed.",
                "type": "implementation_note",
                "tags": ["gobby", "build-e2e", "#14353"],
            }
        )
        + "\n"
        + json.dumps({"content": "durable memory", "type": "fact"})
        + "\n"
    )

    count = await sync_manager.import_from_files()

    assert count == 1
    sync_manager.memory_manager.storage.create_memory.assert_called_once()
    call_args = sync_manager.memory_manager.storage.create_memory.call_args.kwargs
    assert call_args["content"] == "durable memory"


@pytest.mark.asyncio
async def test_get_export_path_context_error(sync_manager):
    """Test _get_export_path falls back to cwd on context error."""
    sync_manager.export_path = Path(".gobby/memories.jsonl")

    with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
        mock_ctx.side_effect = Exception("Context error")

        path = sync_manager._get_export_path()

    # Should fall back to cwd
    assert path == Path.cwd() / ".gobby/memories.jsonl"


@pytest.mark.asyncio
async def test_get_export_path_no_context_path(sync_manager):
    """Test _get_export_path falls back to cwd when context has no project_path."""
    sync_manager.export_path = Path(".gobby/memories.jsonl")

    with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"name": "test"}  # No "project_path" key

        path = sync_manager._get_export_path()

    # Should fall back to cwd
    assert path == Path.cwd() / ".gobby/memories.jsonl"


@pytest.mark.asyncio
async def test_import_from_files_disabled(mock_db, mock_memory_manager):
    """Test import_from_files returns 0 when disabled."""
    config = MemoryBackupConfig(enabled=False)
    sync_manager = MemorySyncManager(mock_db, mock_memory_manager, config)

    count = await sync_manager.import_from_files()

    assert count == 0


@pytest.mark.asyncio
async def test_import_from_files_no_memory_manager(mock_db):
    """Test import_from_files returns 0 when no memory manager."""
    config = MemoryBackupConfig(enabled=True)
    sync_manager = MemorySyncManager(mock_db, None, config)

    count = await sync_manager.import_from_files()

    assert count == 0


@pytest.mark.asyncio
async def test_import_from_files_not_exists(sync_manager, tmp_path):
    """Test import_from_files returns 0 when file doesn't exist."""
    sync_manager.export_path = tmp_path / "nonexistent.jsonl"

    count = await sync_manager.import_from_files()

    assert count == 0


@pytest.mark.asyncio
async def test_import_skips_empty_lines(sync_manager, tmp_path):
    """Test import skips empty lines."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    # File with empty lines
    mem_file.write_text(
        "\n"
        + json.dumps({"content": "memory1", "type": "fact"})
        + "\n"
        + "\n"
        + "   \n"
        + json.dumps({"content": "memory2", "type": "fact"})
        + "\n"
    )

    count = await sync_manager.import_from_files()

    assert count == 2


@pytest.mark.asyncio
async def test_import_skips_duplicates(sync_manager, tmp_path):
    """Test import skips duplicate memories."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    # Make content_exists return True for duplicate
    sync_manager.memory_manager.content_exists.side_effect = [True, False]

    mem_file.write_text(
        json.dumps({"content": "duplicate", "type": "fact"})
        + "\n"
        + json.dumps({"content": "new", "type": "fact"})
        + "\n"
    )

    count = await sync_manager.import_from_files()

    # Only 1 should be imported
    assert count == 1
    assert sync_manager.memory_manager.storage.create_memory.call_count == 1


@pytest.mark.asyncio
async def test_import_handles_invalid_json(sync_manager, tmp_path, caplog):
    """Test import handles invalid JSON lines."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    mem_file.write_text(
        "not valid json\n" + json.dumps({"content": "valid", "type": "fact"}) + "\n"
    )

    count = await sync_manager.import_from_files()

    assert count == 1
    assert "Invalid JSON in memories file" in caplog.text


@pytest.mark.asyncio
async def test_import_handles_create_error(sync_manager, tmp_path, caplog):
    """Test import handles create_memory errors."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    sync_manager.memory_manager.storage.create_memory.side_effect = [
        Exception("Create failed"),
        MagicMock(),  # Second call succeeds
    ]

    mem_file.write_text(
        json.dumps({"content": "memory1", "type": "fact"})
        + "\n"
        + json.dumps({"content": "memory2", "type": "fact"})
        + "\n"
    )

    count = await sync_manager.import_from_files()

    # Only second one succeeded
    assert count == 1


@pytest.mark.asyncio
async def test_import_handles_file_error(sync_manager, tmp_path, caplog):
    """Test import handles file read errors."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    # Create file then make it unreadable (simulated via mock)
    mem_file.write_text("test\n")

    with patch("builtins.open", side_effect=OSError("Permission denied")):
        count = await sync_manager.import_from_files()

    assert count == 0
    assert "Failed to import memories" in caplog.text


def test_export_sync(sync_manager, tmp_path) -> None:
    """Test synchronous export."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file

    count = sync_manager.export_sync()

    assert count == 1
    assert mem_file.exists()


def test_export_sync_disabled(mock_db, mock_memory_manager) -> None:
    """Test export_sync returns 0 when disabled."""
    config = MemoryBackupConfig(enabled=False)
    sync_manager = MemorySyncManager(mock_db, mock_memory_manager, config)

    count = sync_manager.export_sync()

    assert count == 0


def test_export_sync_no_memory_manager(mock_db) -> None:
    """Test export_sync returns 0 when no memory manager."""
    config = MemoryBackupConfig(enabled=True)
    sync_manager = MemorySyncManager(mock_db, None, config)

    count = sync_manager.export_sync()

    assert count == 0


def test_export_sync_error(sync_manager, caplog) -> None:
    """Test export_sync handles errors."""
    sync_manager.memory_manager.list_memories.side_effect = Exception("Export failed")

    count = sync_manager.export_sync()

    assert count == 0
    assert "Failed to export memories" in caplog.text


@pytest.mark.asyncio
async def test_export_to_files_disabled(mock_db, mock_memory_manager):
    """Test export_to_files returns 0 when disabled."""
    config = MemoryBackupConfig(enabled=False)
    sync_manager = MemorySyncManager(mock_db, mock_memory_manager, config)

    count = await sync_manager.export_to_files()

    assert count == 0


@pytest.mark.asyncio
async def test_export_to_files_no_memory_manager(mock_db):
    """Test export_to_files returns 0 when no memory manager."""
    config = MemoryBackupConfig(enabled=True)
    sync_manager = MemorySyncManager(mock_db, None, config)

    count = await sync_manager.export_to_files()

    assert count == 0


@pytest.mark.asyncio
async def test_export_skips_ephemeral_implementation_notes(sync_manager, tmp_path):
    """Run-specific implementation notes are dropped from DB and file export."""
    mem_file = tmp_path / "memories.jsonl"
    sync_manager.export_path = mem_file
    mem_file.write_text(
        json.dumps(
            {
                "id": "file-ephemeral",
                "content": "Gobby build #epic E2E docs test #14353 completed.",
                "type": "implementation_note",
                "tags": ["build-e2e"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sync_manager.memory_manager.list_memories.return_value = [
        Memory(
            id="db-ephemeral",
            content="Gobby build #epic E2E docs test #14353 completed.",
            memory_type="implementation_note",
            created_at="2026-05-07T00:00:00Z",
            updated_at="2026-05-07T00:00:00Z",
            access_count=0,
            last_accessed_at=None,
            tags=["gobby", "build-e2e", "#14353"],
            project_id="p1",
            source_type="agent",
            source_session_id=None,
        ),
        Memory(
            id="db-durable",
            content="durable memory",
            memory_type="fact",
            created_at="2026-05-07T00:00:00Z",
            updated_at="2026-05-07T00:00:00Z",
            access_count=0,
            last_accessed_at=None,
            tags=[],
            project_id="p1",
            source_type="agent",
            source_session_id=None,
        ),
    ]

    count = await sync_manager.export_to_files()

    lines = mem_file.read_text(encoding="utf-8").splitlines()
    assert count == 1
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "db-durable"


def test_import_memories_sync_no_manager(mock_db, tmp_path) -> None:
    """Test _import_memories_sync returns 0 when no manager."""
    config = MemoryBackupConfig(enabled=True)
    sync_manager = MemorySyncManager(mock_db, None, config)

    count = sync_manager._import_memories_sync(tmp_path / "test.jsonl")

    assert count == 0


def test_export_memories_sync_no_manager(mock_db, tmp_path) -> None:
    """Test _export_memories_sync returns 0 when no manager."""
    config = MemoryBackupConfig(enabled=True)
    sync_manager = MemorySyncManager(mock_db, None, config)

    count = sync_manager._export_memories_sync(tmp_path / "test.jsonl")

    assert count == 0


def test_export_memories_sync_error(sync_manager, tmp_path, caplog) -> None:
    """Test _export_memories_sync handles errors."""
    sync_manager.memory_manager.list_memories.side_effect = Exception("List failed")

    count = sync_manager._export_memories_sync(tmp_path / "test.jsonl")

    assert count == 0
    assert "Failed to export memories" in caplog.text


# =============================================================================
# TDD RED PHASE: Tests for backup-only refactoring
# These tests define expected behavior for the MemoryBackupManager rename
# =============================================================================


class TestBackupManagerRename:
    """Tests for MemoryBackupManager class rename and API updates."""

    def test_memory_backup_manager_is_exported(self) -> None:
        """Test that MemoryBackupManager is exported in __all__."""
        from gobby.sync import memories

        assert hasattr(memories, "__all__")
        assert "MemoryBackupManager" in memories.__all__

    def test_memory_backup_manager_direct_import(self) -> None:
        """Test that MemoryBackupManager can be imported directly."""
        from gobby.sync.memories import MemoryBackupManager

        assert MemoryBackupManager is not None

    def test_backup_sync_method_exists(self, sync_manager) -> None:
        """Test that backup_sync method exists (renamed from export_sync)."""
        assert hasattr(sync_manager, "backup_sync")
        assert callable(sync_manager.backup_sync)

    def test_backup_sync_method_works(self, sync_manager, tmp_path) -> None:
        """Test backup_sync method functions correctly."""
        mem_file = tmp_path / "memories.jsonl"
        sync_manager.export_path = mem_file

        count = sync_manager.backup_sync()

        assert count == 1
        assert mem_file.exists()

    def test_backup_sync_disabled(self, mock_db, mock_memory_manager) -> None:
        """Test backup_sync returns 0 when disabled."""
        config = MemoryBackupConfig(enabled=False)
        manager = MemorySyncManager(mock_db, mock_memory_manager, config)

        count = manager.backup_sync()

        assert count == 0

    def test_backup_sync_no_memory_manager(self, mock_db) -> None:
        """Test backup_sync returns 0 when no memory manager."""
        config = MemoryBackupConfig(enabled=True)
        manager = MemorySyncManager(mock_db, None, config)

        count = manager.backup_sync()

        assert count == 0


class TestExportMerge:
    """Tests for merge-aware export (preserves file-only records)."""

    @pytest.fixture
    def manager_with_memories(self, mock_db):
        """Create a sync manager whose DB returns memories B and C."""
        mm = MagicMock()
        mm.list_memories = MagicMock(
            return_value=[
                Memory(
                    id="m-b",
                    content="memory B",
                    memory_type="fact",
                    created_at="2023-01-02T00:00:00Z",
                    updated_at="2023-01-02T00:00:00Z",
                    access_count=0,
                    last_accessed_at=None,
                    tags=["db"],
                    project_id="p1",
                    source_type="user",
                    source_session_id=None,
                ),
                Memory(
                    id="m-c",
                    content="memory C",
                    memory_type="fact",
                    created_at="2023-01-03T00:00:00Z",
                    updated_at="2023-01-03T00:00:00Z",
                    access_count=0,
                    last_accessed_at=None,
                    tags=["db"],
                    project_id="p1",
                    source_type="user",
                    source_session_id=None,
                ),
            ]
        )
        config = MemoryBackupConfig(enabled=True, export_debounce=0.1)
        return MemorySyncManager(mock_db, mm, config)

    def test_export_merges_with_existing_file(self, manager_with_memories, tmp_path) -> None:
        """File has A, B (old). DB has B, C. Result: A (file), B (DB version), C (DB)."""
        mem_file = tmp_path / "memories.jsonl"
        # Write file with records A and B (file version)
        file_records = [
            {"id": "m-a", "content": "memory A", "type": "fact", "tags": ["file"]},
            {"id": "m-b-old", "content": "memory B", "type": "fact", "tags": ["file"]},
        ]
        with open(mem_file, "w") as f:
            for rec in file_records:
                f.write(json.dumps(rec) + "\n")

        manager_with_memories.export_path = mem_file
        count = manager_with_memories.backup_sync()

        assert count == 3  # A (file-only) + B (DB override) + C (DB-only)

        # Parse results
        with open(mem_file) as f:
            results = {json.loads(line)["content"].strip(): json.loads(line) for line in f}

        # A preserved from file
        assert "memory A" in results
        assert results["memory A"]["tags"] == ["file"]

        # B overridden by DB version
        assert "memory B" in results
        assert results["memory B"]["tags"] == ["db"]
        assert results["memory B"]["id"] == "m-b"

        # C added from DB
        assert "memory C" in results

    def test_export_preserves_file_only_records(self, mock_db, tmp_path) -> None:
        """File-only records survive even when DB is empty."""
        mm = MagicMock()
        mm.list_memories = MagicMock(return_value=[])
        config = MemoryBackupConfig(enabled=True, export_debounce=0.1)
        manager = MemorySyncManager(mock_db, mm, config)

        mem_file = tmp_path / "memories.jsonl"
        file_records = [
            {"id": "m-x", "content": "remote memory X", "type": "fact", "tags": ["remote"]},
            {"id": "m-y", "content": "remote memory Y", "type": "fact", "tags": ["remote"]},
        ]
        with open(mem_file, "w") as f:
            for rec in file_records:
                f.write(json.dumps(rec) + "\n")

        manager.export_path = mem_file
        count = manager.backup_sync()

        assert count == 2
        with open(mem_file) as f:
            contents = [json.loads(line)["content"] for line in f]
        assert "remote memory X" in contents
        assert "remote memory Y" in contents

    def test_export_handles_corrupt_file_lines(self, manager_with_memories, tmp_path) -> None:
        """Corrupt JSON lines in existing file are skipped, valid lines preserved."""
        mem_file = tmp_path / "memories.jsonl"
        with open(mem_file, "w") as f:
            f.write('{"id": "m-a", "content": "memory A", "type": "fact", "tags": ["file"]}\n')
            f.write("this is not valid json\n")
            f.write('{"id": "m-d", "content": "memory D", "type": "fact", "tags": ["file"]}\n')
            f.write("\n")  # empty line

        manager_with_memories.export_path = mem_file
        count = manager_with_memories.backup_sync()

        # A + D from file, B + C from DB = 4
        assert count == 4
        with open(mem_file) as f:
            contents = {json.loads(line)["content"].strip() for line in f}
        assert contents == {"memory A", "memory B", "memory C", "memory D"}
