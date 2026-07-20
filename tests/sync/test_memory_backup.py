"""Tests for deterministic memory backups and explicit non-destructive restore."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.persistence import MemoryBackupConfig, MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.sync.memories import MemoryBackupError, MemoryBackupManager, MemoryRestoreError

pytestmark = pytest.mark.integration

MEMORY_A = "00000000-0000-0000-0000-000000000001"
MEMORY_B = "00000000-0000-0000-0000-000000000002"
MEMORY_C = "00000000-0000-0000-0000-000000000003"
OLD_TIME = datetime(2024, 1, 1, tzinfo=UTC)
NEW_TIME = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def memory_manager(hub_db: HubDatabase) -> MemoryManager:
    return MemoryManager(
        db=hub_db,
        config=MemoryConfig(enabled=True, backend="local", access_debounce_seconds=0),
    )


@pytest.fixture
def backup_path(tmp_path: Path) -> Path:
    return tmp_path / "memories.jsonl"


@pytest.fixture
def backup_manager(
    hub_db: HubDatabase,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> MemoryBackupManager:
    return MemoryBackupManager(
        db=hub_db,
        memory_manager=memory_manager,
        config=MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )


def _create_memory(
    memory_manager: MemoryManager,
    *,
    memory_id: str,
    content: str,
    updated_at: datetime = OLD_TIME,
) -> None:
    memory_manager.storage.create_memory(
        content=content,
        memory_type="fact",
        source_type="agent",
        memory_id=memory_id,
        created_at=OLD_TIME,
        updated_at=updated_at,
    )


def _record(
    *,
    memory_id: str,
    content: str,
    updated_at: datetime = OLD_TIME,
) -> dict[str, object]:
    return {
        "id": memory_id,
        "content": content,
        "type": "fact",
        "tags": [],
        "created_at": OLD_TIME.isoformat(),
        "updated_at": updated_at.isoformat(),
        "source": "agent",
        "source_id": None,
        "project_id": None,
    }


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_backup_contains_only_live_rows_in_deterministic_order_and_shrinks(
    backup_manager: MemoryBackupManager,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> None:
    _create_memory(memory_manager, memory_id=MEMORY_B, content="memory B")
    _create_memory(memory_manager, memory_id=MEMORY_A, content="memory A")

    assert backup_manager.backup_sync() == 2
    first_content = backup_path.read_text(encoding="utf-8")
    first_records = [json.loads(line) for line in first_content.splitlines()]
    assert [record["id"] for record in first_records] == [MEMORY_A, MEMORY_B]
    assert all("_deleted" not in record for record in first_records)

    assert backup_manager.backup_sync() == 2
    assert backup_path.read_text(encoding="utf-8") == first_content

    assert memory_manager.storage.delete_memory(MEMORY_A)
    assert backup_manager.backup_sync() == 1
    remaining = [json.loads(line) for line in backup_path.read_text().splitlines()]
    assert [record["id"] for record in remaining] == [MEMORY_B]


def test_backup_replaces_previous_file_instead_of_merging(
    backup_manager: MemoryBackupManager,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> None:
    _create_memory(memory_manager, memory_id=MEMORY_A, content="database memory")
    _write_records(backup_path, [_record(memory_id=MEMORY_C, content="file only")])

    assert backup_manager.backup_sync() == 1
    records = [json.loads(line) for line in backup_path.read_text().splitlines()]
    assert [record["id"] for record in records] == [MEMORY_A]


def test_backup_publish_is_atomic(
    backup_manager: MemoryBackupManager,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> None:
    _create_memory(memory_manager, memory_id=MEMORY_A, content="database memory")
    original = b'{"existing":true}\n'
    backup_path.write_bytes(original)

    with patch("gobby.sync.jsonl_io.os.replace", side_effect=OSError("interrupted")):
        with pytest.raises(MemoryBackupError, match="Failed to write memory backup: interrupted"):
            backup_manager.backup_sync()

    assert backup_path.read_bytes() == original


def test_restore_creates_missing_and_preserves_rows_absent_from_backup(
    backup_manager: MemoryBackupManager,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> None:
    _create_memory(memory_manager, memory_id=MEMORY_A, content="database only")
    _write_records(backup_path, [_record(memory_id=MEMORY_B, content="restored")])

    assert backup_manager.restore_sync() == 1
    rows = backup_manager.db.fetchall("SELECT id, content FROM memories ORDER BY id")
    assert [(row["id"], row["content"]) for row in rows] == [
        (MEMORY_A, "database only"),
        (MEMORY_B, "restored"),
    ]


def test_restore_updates_only_when_backup_timestamp_wins(
    backup_manager: MemoryBackupManager,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> None:
    _create_memory(memory_manager, memory_id=MEMORY_A, content="old database")
    _create_memory(
        memory_manager,
        memory_id=MEMORY_B,
        content="new database",
        updated_at=NEW_TIME,
    )
    _write_records(
        backup_path,
        [
            _record(memory_id=MEMORY_A, content="new backup", updated_at=NEW_TIME),
            _record(memory_id=MEMORY_B, content="old backup", updated_at=OLD_TIME),
        ],
    )

    assert backup_manager.restore_sync() == 1
    rows = backup_manager.db.fetchall("SELECT id, content FROM memories ORDER BY id")
    assert [(row["id"], row["content"]) for row in rows] == [
        (MEMORY_A, "new backup"),
        (MEMORY_B, "new database"),
    ]


def test_older_backup_does_not_reactivate_newer_hidden_memory(
    backup_manager: MemoryBackupManager,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> None:
    _create_memory(
        memory_manager,
        memory_id=MEMORY_A,
        content="new database",
        updated_at=NEW_TIME,
    )
    backup_manager.db.execute(
        "UPDATE memories SET deleted_at = %s, updated_at = %s WHERE id = %s",
        (NEW_TIME, NEW_TIME, MEMORY_A),
    )
    _write_records(
        backup_path,
        [_record(memory_id=MEMORY_A, content="old backup", updated_at=OLD_TIME)],
    )

    assert backup_manager.restore_sync() == 0
    row = backup_manager.db.fetchone(
        "SELECT content, deleted_at FROM memories WHERE id = %s",
        (MEMORY_A,),
    )
    assert row["content"] == "new database"
    assert row["deleted_at"] is not None


@pytest.mark.parametrize(
    "bad_line",
    [
        "{malformed json",
        json.dumps({"id": MEMORY_B, "_deleted": True}),
    ],
    ids=["malformed", "tombstone"],
)
def test_invalid_record_aborts_restore_before_any_mutation(
    bad_line: str,
    backup_manager: MemoryBackupManager,
    backup_path: Path,
) -> None:
    valid = json.dumps(_record(memory_id=MEMORY_A, content="would be inserted"))
    backup_path.write_text(f"{valid}\n{bad_line}\n", encoding="utf-8")

    with pytest.raises(MemoryRestoreError):
        backup_manager.restore_sync()

    assert backup_manager.db.fetchone("SELECT id FROM memories WHERE id = %s", (MEMORY_A,)) is None


def test_restore_missing_file_and_disabled_manager_are_noops(
    hub_db: HubDatabase,
    memory_manager: MemoryManager,
    backup_path: Path,
) -> None:
    enabled = MemoryBackupManager(
        hub_db,
        memory_manager,
        MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )
    disabled = MemoryBackupManager(
        hub_db,
        memory_manager,
        MemoryBackupConfig(enabled=False, backup_path=backup_path),
    )

    assert enabled.restore_sync() == 0
    assert disabled.backup_sync() == 0
    assert not backup_path.exists()
