from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryBackupConfig, MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.storage.memories_models import PERSONAL_PROJECT_ID
from gobby.sync.memories import MemoryBackupManager


@pytest.mark.asyncio
async def test_import_fresh_and_winning_writes_reconcile_before_result(
    hub_db,
    tmp_path,
) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    manager = MemoryManager(
        db=hub_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.1, 0.2]),
    )
    backup_path = tmp_path / "memories.jsonl"
    timestamp = datetime(2026, 7, 22, tzinfo=UTC).isoformat()
    record = {
        "id": "11111111-2222-4333-8444-555555555555",
        "content": "fresh imported memory",
        "type": "fact",
        "tags": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "source": "agent",
        "source_id": None,
        "project_id": PERSONAL_PROJECT_ID,
        "is_global": False,
    }
    backup_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    backup = MemoryBackupManager(
        db=hub_db,
        memory_manager=manager,
        config=MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )

    assert await backup.restore() == 1

    memory = manager.storage.get_memory(record["id"])
    assert memory.vector_needs_reindex is False
    assert memory.graph_status == "pending"
    vector_store.upsert.assert_awaited_once_with(
        record["id"],
        [0.1, 0.2],
        {
            "project_id": PERSONAL_PROJECT_ID,
            "is_global": False,
            "memory_type": "fact",
        },
    )


@pytest.mark.asyncio
async def test_export_canonicalizes_oversized_tombstone_project_id(
    hub_db,
    tmp_path,
) -> None:
    """Backup export never re-emits retired merge-sync tombstone records."""
    backup_path = tmp_path / "memories.jsonl"
    backup_path.write_text(
        json.dumps(
            {
                "id": "11111111-2222-4333-8444-555555555555",
                "project_id": "x" * 100_000,
                "_deleted": True,
                "deleted_at": datetime(2026, 7, 22, tzinfo=UTC).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manager = MemoryManager(db=hub_db, config=MemoryConfig(enabled=True))
    backup = MemoryBackupManager(
        db=hub_db,
        memory_manager=manager,
        config=MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )

    assert await backup.backup() == 0
    assert backup_path.read_text(encoding="utf-8") == ""
