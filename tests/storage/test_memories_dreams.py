from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_scope import ALL_MEMORIES
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit


def test_list_dream_candidate_ids_snapshot_order(temp_db: HubDatabase) -> None:
    manager = LocalMemoryManager(temp_db)
    memories = [
        manager.create_memory(content=f"snapshot candidate {index}", project_id=PERSONAL_PROJECT_ID)
        for index in range(3)
    ]
    shared_time = datetime.now(UTC) - timedelta(days=2)
    temp_db.execute(
        "UPDATE memories SET updated_at = %s, last_dreamed_at = %s WHERE id = ANY(%s)",
        (shared_time, shared_time, [memory.id for memory in memories]),
    )

    candidate_ids = manager.list_dream_candidate_ids(
        redream_cutoff=datetime.now(UTC),
        scope=ALL_MEMORIES,
    )

    assert candidate_ids == sorted(memory.id for memory in memories)
