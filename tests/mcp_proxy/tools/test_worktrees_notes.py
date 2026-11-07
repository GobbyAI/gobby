"""The gobby-worktrees note tool records owner, next action and blocker for a registered tree."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktree_notes import WorktreeNoteStore
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit


@pytest.fixture
def tool_setup(temp_db: HubDatabase, sample_project: dict[str, Any]) -> tuple[Any, str]:
    project_id = str(sample_project["id"])
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-1", "/repo/worktrees/task-1")
    registry = create_worktrees_registry(LocalWorktreeManager(temp_db), project_id=project_id)
    return registry, tree.id


@pytest.mark.asyncio
async def test_set_note_persists_owner_next_action_and_blocker(
    temp_db: HubDatabase, tool_setup: tuple[Any, str]
) -> None:
    registry, tree = tool_setup
    before = datetime.now(UTC)

    result = await registry.call(
        "set_worktree_note",
        {
            "worktree_id": tree,
            "owner": "dev-1",
            "next_action": "rerun the focused tests",
            "blocker": "waiting on review",
        },
    )

    assert result["success"] is True
    note = WorktreeNoteStore(temp_db).list_for([tree])[tree]
    assert (note.owner, note.next_action, note.blocker) == (
        "dev-1",
        "rerun the focused tests",
        "waiting on review",
    )
    assert note.updated_at >= before


@pytest.mark.asyncio
async def test_empty_blocker_records_no_blocker(
    temp_db: HubDatabase, tool_setup: tuple[Any, str]
) -> None:
    registry, tree = tool_setup

    result = await registry.call(
        "set_worktree_note",
        {"worktree_id": tree, "owner": "dev-1", "next_action": "merge", "blocker": ""},
    )

    assert result["success"] is True
    assert WorktreeNoteStore(temp_db).list_for([tree])[tree].blocker == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "next_action", "blocker"),
    [
        ("", "merge", ""),
        ("dev-1", "   ", ""),
        ("dev-1", "line one\nline two", ""),
        ("dev-1", "merge", "first\nsecond"),
    ],
)
async def test_blank_or_multiline_fields_are_refused(
    temp_db: HubDatabase,
    tool_setup: tuple[Any, str],
    owner: str,
    next_action: str,
    blocker: str,
) -> None:
    registry, tree = tool_setup

    result = await registry.call(
        "set_worktree_note",
        {"worktree_id": tree, "owner": owner, "next_action": next_action, "blocker": blocker},
    )

    assert result["success"] is False
    assert tree not in WorktreeNoteStore(temp_db).list_for([tree])


@pytest.mark.asyncio
async def test_unknown_worktree_is_refused(
    temp_db: HubDatabase, tool_setup: tuple[Any, str]
) -> None:
    registry, _ = tool_setup

    result = await registry.call(
        "set_worktree_note",
        {
            "worktree_id": "00000000-0000-4000-8000-000000000000",
            "owner": "dev-1",
            "next_action": "merge",
            "blocker": "",
        },
    )

    assert result["success"] is False
    assert "error" in result
