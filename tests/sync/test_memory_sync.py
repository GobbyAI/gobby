from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryBackupConfig, MemoryConfig
from gobby.memory.dream.models import DreamCandidate, RelatedMemoryEvidence
from gobby.memory.manager import MemoryManager
from gobby.memory.services import lifecycle as lifecycle_module
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories_models import PERSONAL_PROJECT_ID
from gobby.sync.memories import MemoryBackupManager


@pytest.mark.asyncio
async def test_import_created_reactivated_marks_older_due(
    hub_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public JSONL restore awaits shared hooks for fresh and reactivated knowledge."""
    manager = MemoryManager(db=hub_db, config=MemoryConfig(enabled=True))
    created_at = datetime(2025, 1, 1, tzinfo=UTC)
    intervening_at = datetime(2025, 6, 1, tzinfo=UTC)
    imported_at = datetime(2026, 1, 1, tzinfo=UTC)
    reactivated = manager.storage.create_memory(
        content="reactivated import",
        project_id=PERSONAL_PROJECT_ID,
        memory_id="11111111-1111-4111-8111-111111111111",
        created_at=created_at,
        updated_at=created_at,
    )
    manager.storage.mark_dreamed(reactivated.id, hidden_as="review", when=created_at)
    older = manager.storage.create_memory(
        content="older related survivor",
        project_id=PERSONAL_PROJECT_ID,
        created_at=intervening_at,
        updated_at=intervening_at,
    )
    manager.storage.mark_dreamed(older.id, when=intervening_at)
    completed_hooks: list[str] = []

    async def related(
        candidates: list[DreamCandidate],
        **kwargs: Any,
    ) -> list[DreamCandidate]:
        candidate = candidates[0]
        if candidate.content == "reactivated import":
            assert kwargs["anchor_at"] == candidate.updated_at
            assert kwargs["anchor_at"] > older.created_at
        else:
            assert kwargs["anchor_at"] == candidate.created_at
        evidence = RelatedMemoryEvidence(
            id=older.id,
            memory_type="fact",
            created_at=older.created_at,
            newer_by_days=0.0,
            content=older.content,
            matched_via="keyword+vector",
        )
        completed_hooks.append(candidate.content)
        return [replace(candidate, related=(evidence,))]

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", related)
    records = [
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "content": "fresh import",
            "type": "fact",
            "tags": [],
            "created_at": imported_at.isoformat(),
            "updated_at": imported_at.isoformat(),
            "source": "agent",
            "source_id": None,
            "project_id": PERSONAL_PROJECT_ID,
            "is_global": False,
        },
        {
            "id": reactivated.id,
            "content": "reactivated import",
            "type": "fact",
            "tags": [],
            "created_at": created_at.isoformat(),
            "updated_at": imported_at.isoformat(),
            "source": "agent",
            "source_id": None,
            "project_id": PERSONAL_PROJECT_ID,
            "is_global": False,
        },
    ]
    backup_path = tmp_path / "mark-due.jsonl"
    backup_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    backup = MemoryBackupManager(
        db=hub_db,
        memory_manager=manager,
        config=MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )

    assert await backup.restore() == 2

    assert sorted(completed_hooks) == ["fresh import", "reactivated import"]
    assert manager.storage.get_memory(older.id).last_dreamed_at is None
    assert manager.storage.get_memory(older.id).dream_due_version == 2
    assert manager._background_tasks == set()


@pytest.mark.asyncio
async def test_import_updated_unchanged_deduped_no_mark_due(
    hub_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only new active knowledge owns the import-time wakeup hook."""
    manager = MemoryManager(db=hub_db, config=MemoryConfig(enabled=True))
    existing_at = datetime(2025, 1, 1, tzinfo=UTC)
    imported_at = datetime(2026, 1, 1, tzinfo=UTC)
    existing = manager.storage.create_memory(
        content="existing import row",
        project_id=PERSONAL_PROJECT_ID,
        memory_id="33333333-3333-4333-8333-333333333333",
        created_at=existing_at,
        updated_at=existing_at,
    )
    duplicate = manager.storage.create_memory(
        content="dedup import content",
        project_id=PERSONAL_PROJECT_ID,
    )
    manager.storage.mark_dreamed(duplicate.id, when=existing_at)
    assert manager.schedule_write_mark_due(duplicate, "deduped") is None
    calls = 0

    async def should_not_run(
        candidates: list[DreamCandidate],
        **_kwargs: Any,
    ) -> list[DreamCandidate]:
        nonlocal calls
        calls += 1
        return candidates

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", should_not_run)
    records = [
        {
            "id": existing.id,
            "content": "updated import row",
            "type": "fact",
            "tags": [],
            "created_at": existing_at.isoformat(),
            "updated_at": imported_at.isoformat(),
            "source": "agent",
            "source_id": None,
            "project_id": PERSONAL_PROJECT_ID,
            "is_global": False,
        },
        {
            "id": duplicate.id,
            "content": "dedup import content",
            "type": "fact",
            "tags": [],
            "created_at": existing_at.isoformat(),
            "updated_at": existing_at.isoformat(),
            "source": "agent",
            "source_id": None,
            "project_id": PERSONAL_PROJECT_ID,
            "is_global": False,
        },
    ]
    backup_path = tmp_path / "no-mark-due.jsonl"
    backup_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    backup = MemoryBackupManager(
        db=hub_db,
        memory_manager=manager,
        config=MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )

    assert await backup.restore() == 1
    assert calls == 0
    assert manager.storage.get_memory(duplicate.id).last_dreamed_at is not None


@pytest.mark.asyncio
async def test_import_fresh_and_winning_writes_reconcile_before_result(
    hub_db: HubDatabase,
    tmp_path: Path,
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
    hub_db: HubDatabase,
    tmp_path: Path,
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


@pytest.mark.asyncio
async def test_restore_reconciliation_is_bounded_and_concurrent(
    hub_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_path = tmp_path / "memories.jsonl"
    backup_path.write_text("{}\n", encoding="utf-8")
    manager = MagicMock()
    active = 0
    peak_active = 0

    async def reconcile_memory_indices(_memory_id: str) -> None:
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.001)
        active -= 1

    manager.reconcile_memory_indices = AsyncMock(side_effect=reconcile_memory_indices)
    manager.schedule_write_mark_due.return_value = None
    outcomes = [
        MemoryWriteResult(memory=MagicMock(id=f"memory-{index}"), outcome="created")
        for index in range(20)
    ]
    backup = MemoryBackupManager(
        db=hub_db,
        memory_manager=manager,
        config=MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )
    monkeypatch.setattr(
        backup,
        "_restore_memories_with_outcomes_sync",
        lambda _path: (len(outcomes), outcomes),
    )

    assert await backup.restore() == 20
    assert 1 < peak_active <= 8


@pytest.mark.asyncio
async def test_restore_cancellation_waits_for_owned_reconciliation(
    hub_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_path = tmp_path / "memories.jsonl"
    backup_path.write_text("{}\n", encoding="utf-8")
    manager = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def reconcile_memory_indices(_memory_id: str) -> None:
        started.set()
        await release.wait()
        finished.set()

    manager.reconcile_memory_indices = AsyncMock(side_effect=reconcile_memory_indices)
    manager.schedule_write_mark_due.return_value = None
    outcome = MemoryWriteResult(memory=MagicMock(id="memory-1"), outcome="created")
    backup = MemoryBackupManager(
        db=hub_db,
        memory_manager=manager,
        config=MemoryBackupConfig(enabled=True, backup_path=backup_path),
    )
    monkeypatch.setattr(
        backup,
        "_restore_memories_with_outcomes_sync",
        lambda _path: (1, [outcome]),
    )
    caller = asyncio.create_task(backup.restore())
    await started.wait()

    caller.cancel()
    await asyncio.sleep(0)
    assert not finished.is_set()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert finished.is_set()
