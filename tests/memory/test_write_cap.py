"""Write-path content cap for memories.

Deliverable 3.1 of `.gobby/plans/memory-injection-redesign.md`: memory content is
bounded at write time so an oversize body can never reach the injection budget.
The cap is enforced on every lifecycle write entry point, not only the two the
plan names, because the MCP `update_memory` tool reaches storage through
`update_memory_scoped` and the async backend path reaches it through
`aupdate_memory`.
"""

from __future__ import annotations

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.memory.services.lifecycle import MAX_MEMORY_CONTENT_CHARS
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.integration


AT_CAP = "x" * MAX_MEMORY_CONTENT_CHARS
OVER_CAP = "x" * (MAX_MEMORY_CONTENT_CHARS + 1)


def _manager(temp_db: HubDatabase) -> MemoryManager:
    return MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))


def test_cap_is_three_thousand() -> None:
    """3,000 is set from the live distribution: p99 is 2,468 and p99.9 is 3,179."""
    assert MAX_MEMORY_CONTENT_CHARS == 3000


@pytest.mark.asyncio
async def test_create_memory_rejects_content_over_cap(temp_db: HubDatabase) -> None:
    manager = _manager(temp_db)

    with pytest.raises(ValueError) as excinfo:
        await manager.create_memory(content=OVER_CAP, project_id=PERSONAL_PROJECT_ID)

    message = str(excinfo.value)
    assert str(MAX_MEMORY_CONTENT_CHARS + 1) in message, message
    assert str(MAX_MEMORY_CONTENT_CHARS) in message, message


@pytest.mark.asyncio
async def test_create_memory_accepts_content_at_cap(temp_db: HubDatabase) -> None:
    manager = _manager(temp_db)

    memory = await manager.create_memory(content=AT_CAP, project_id=PERSONAL_PROJECT_ID)

    assert len(memory.content) == MAX_MEMORY_CONTENT_CHARS


@pytest.mark.asyncio
async def test_update_memory_rejects_content_over_cap(temp_db: HubDatabase) -> None:
    manager = _manager(temp_db)
    existing = manager.storage.create_memory(
        content="A short memory.",
        project_id=PERSONAL_PROJECT_ID,
    )

    with pytest.raises(ValueError) as excinfo:
        await manager.update_memory(memory_id=existing.id, content=OVER_CAP)

    message = str(excinfo.value)
    assert str(MAX_MEMORY_CONTENT_CHARS + 1) in message, message
    assert str(MAX_MEMORY_CONTENT_CHARS) in message, message


@pytest.mark.asyncio
async def test_update_memory_scoped_rejects_content_over_cap(temp_db: HubDatabase) -> None:
    """The MCP `update_memory` tool writes through the scoped variant."""
    manager = _manager(temp_db)
    existing = manager.storage.create_memory(
        content="A short memory.",
        project_id=PERSONAL_PROJECT_ID,
    )

    with pytest.raises(ValueError) as excinfo:
        await manager.update_memory_scoped(
            memory_id=existing.id,
            project_id=PERSONAL_PROJECT_ID,
            content=OVER_CAP,
        )

    assert str(MAX_MEMORY_CONTENT_CHARS) in str(excinfo.value)


@pytest.mark.asyncio
async def test_aupdate_memory_rejects_content_over_cap(temp_db: HubDatabase) -> None:
    """The async backend write path is capped too."""
    manager = _manager(temp_db)
    existing = manager.storage.create_memory(
        content="A short memory.",
        project_id=PERSONAL_PROJECT_ID,
    )

    with pytest.raises(ValueError) as excinfo:
        await manager.aupdate_memory(memory_id=existing.id, content=OVER_CAP)

    assert str(MAX_MEMORY_CONTENT_CHARS) in str(excinfo.value)


@pytest.mark.asyncio
async def test_tags_only_update_is_unaffected(temp_db: HubDatabase) -> None:
    """An update carrying no content must not trip the cap."""
    manager = _manager(temp_db)
    existing = manager.storage.create_memory(
        content="A short memory.",
        project_id=PERSONAL_PROJECT_ID,
    )

    updated = await manager.update_memory(memory_id=existing.id, tags=["kept"])

    assert updated.tags is not None
    assert "kept" in updated.tags
