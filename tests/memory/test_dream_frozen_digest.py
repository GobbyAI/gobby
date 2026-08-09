from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.service import MemoryDreamService
from gobby.memory.dream.truth_digest import build_project_truth_digest
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import LocalProjectManager


def _write_frozen_digest(repo_path: Path, project_id: str) -> Path:
    vault = repo_path / "wiki"
    marker = vault / "_gwiki" / "scope.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}\n", encoding="utf-8")

    digest_path = vault / "_meta" / "truth_digest.json"
    digest_path.parent.mkdir(parents=True)
    digest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2000-01-01T00:00:00+00:00",
                "project_id": project_id,
                "repo_summary": "A frozen characterization fixture.",
                "stack_authority": "complete_current_set",
                "stack": [],
                "key_paths": {},
            }
        ),
        encoding="utf-8",
    )
    frozen_timestamp = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
    os.utime(digest_path, (frozen_timestamp, frozen_timestamp))
    return digest_path


@pytest.mark.asyncio
async def test_frozen_digest_is_tolerated(temp_db: HubDatabase, tmp_path: Path) -> None:
    projects = LocalProjectManager(temp_db)
    seen_repo = tmp_path / "seen"
    absent_repo = tmp_path / "absent"
    first_sight_repo = tmp_path / "first-sight"
    seen = projects.create(name="seen-frozen", repo_path=str(seen_repo))
    absent = projects.create(name="absent-digest", repo_path=str(absent_repo))
    first_sight = projects.create(name="first-sight-frozen", repo_path=str(first_sight_repo))

    _write_frozen_digest(seen_repo, seen.id)
    _write_frozen_digest(first_sight_repo, first_sight.id)

    manager = LocalMemoryManager(temp_db)
    dreamed_at = "2026-01-01T00:00:00+00:00"
    memories = {
        project.id: manager.create_memory(content=project.name, project_id=project.id)
        for project in (seen, absent, first_sight)
    }
    for memory in memories.values():
        manager.mark_dreamed(memory.id, when=dreamed_at)

    service = MemoryDreamService(
        memory_manager=cast(MemoryDreamManagerProtocol, manager),
    )
    seen_digest = build_project_truth_digest(str(seen_repo))
    service.store.set_truth_digest_hash(
        seen.id,
        hashlib.sha256(seen_digest.encode("utf-8")).hexdigest(),
    )

    with patch.object(
        manager,
        "mark_project_memories_due",
        wraps=manager.mark_project_memories_due,
    ) as mark_due:
        await service._apply_truth_change_triggers()
        await service._apply_truth_change_triggers()
        await service._apply_truth_change_triggers()

    mark_due.assert_called_once_with(first_sight.id)
    assert manager.get_memory(memories[seen.id].id).last_dreamed_at is not None
    assert manager.get_memory(memories[absent.id].id).last_dreamed_at is not None
    assert manager.get_memory(memories[first_sight.id].id).last_dreamed_at is None
    assert service.store.get_truth_digest_hash(absent.id) is None
    assert service.store.get_truth_digest_hash(first_sight.id) is not None
