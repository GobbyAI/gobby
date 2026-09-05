from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.service import MemoryDreamService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import LocalProjectManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit


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
async def test_legacy_sidecars_do_not_change_project_cooldowns(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase, tmp_path: Path
) -> None:
    projects = LocalProjectManager(temp_db)
    seen_repo = tmp_path / "seen"
    absent_repo = tmp_path / "absent"
    first_sight_repo = tmp_path / "first-sight"
    seen = isolated_checkout_factory(projects.db, "seen-frozen", root=seen_repo).project
    absent = isolated_checkout_factory(projects.db, "absent-digest", root=absent_repo).project
    first_sight = isolated_checkout_factory(
        projects.db, "first-sight-frozen", root=first_sight_repo
    ).project

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
    service.store.set_truth_digest_hash(seen.id, "historical-digest")

    with patch.object(
        manager,
        "mark_project_memories_due",
        wraps=manager.mark_project_memories_due,
    ) as mark_due:
        await service._apply_platform_truth_change_trigger()
        await service._apply_platform_truth_change_trigger()
        await service._apply_platform_truth_change_trigger()

    mark_due.assert_not_called()
    assert manager.get_memory(memories[seen.id].id).last_dreamed_at is not None
    assert manager.get_memory(memories[absent.id].id).last_dreamed_at is not None
    assert manager.get_memory(memories[first_sight.id].id).last_dreamed_at is not None
    assert service.store.get_truth_digest_hash(absent.id) is None
    assert service.store.get_truth_digest_hash(first_sight.id) is None

    assert service.store.get_truth_digest_hash(seen.id) == "historical-digest"
