"""The gobby-worktrees note tool records owner, next action and blocker for a registered tree.

Only a session whose installed definition grants manage_worktree_notes may write a note.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktree_notes import WorktreeNoteStore
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.session_context import (
    SessionContext,
    reset_session_context,
    set_session_context,
)
from tests.fixtures.agent_sessions import AgentSessions
from tests.fixtures.isolated_checkout import insert_isolated_machine

pytestmark = pytest.mark.unit

NOTE_CAPABILITY = "manage_worktree_notes"


@pytest.fixture
def tool_setup(temp_db: HubDatabase, sample_project: dict[str, Any]) -> tuple[Any, str]:
    project_id = str(sample_project["id"])
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-1", "/repo/worktrees/task-1")
    registry = create_worktrees_registry(LocalWorktreeManager(temp_db), project_id=project_id)
    return registry, tree.id


@pytest.fixture
def writer(temp_db: HubDatabase, sample_project: dict[str, Any]) -> Iterator[str]:
    """A session whose definition grants the note capability, set as the caller."""
    session_id = AgentSessions(temp_db, str(sample_project["id"])).add(
        "note-writer", capabilities=(NOTE_CAPABILITY,)
    )
    token = set_session_context(SessionContext(session_id=session_id))
    yield session_id
    reset_session_context(token)


async def _set_note(registry: Any, tree: str) -> dict[str, Any]:
    result: dict[str, Any] = await registry.call(
        "set_worktree_note",
        {"worktree_id": tree, "owner": "dev-1", "next_action": "merge", "blocker": ""},
    )
    return result


@pytest.mark.asyncio
async def test_set_note_persists_owner_next_action_and_blocker(
    temp_db: HubDatabase, tool_setup: tuple[Any, str], writer: str
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
            "eta": "  14:45 CT  ",
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
    assert note.eta == "14:45 CT"


@pytest.mark.asyncio
async def test_empty_blocker_records_no_blocker(
    temp_db: HubDatabase, tool_setup: tuple[Any, str], writer: str
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
    writer: str,
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
    temp_db: HubDatabase, tool_setup: tuple[Any, str], writer: str
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [(), ("land_tasks",)],
    ids=["no-capability", "other-capability"],
)
async def test_caller_without_the_note_capability_is_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tool_setup: tuple[Any, str],
    capabilities: tuple[str, ...],
) -> None:
    registry, tree = tool_setup
    session_id = AgentSessions(temp_db, str(sample_project["id"])).add(
        "plain-worker", capabilities=capabilities
    )
    token = set_session_context(SessionContext(session_id=session_id))
    try:
        result = await _set_note(registry, tree)
    finally:
        reset_session_context(token)

    assert result["success"] is False
    assert tree not in WorktreeNoteStore(temp_db).list_for([tree])


@pytest.mark.asyncio
async def test_call_without_a_session_is_refused(
    temp_db: HubDatabase, tool_setup: tuple[Any, str]
) -> None:
    registry, tree = tool_setup
    token = set_session_context(None)
    try:
        result = await _set_note(registry, tree)
    finally:
        reset_session_context(token)

    assert result["success"] is False
    assert tree not in WorktreeNoteStore(temp_db).list_for([tree])


@pytest.mark.asyncio
async def test_session_from_another_machine_is_refused(
    temp_db: HubDatabase, tool_setup: tuple[Any, str], writer: str
) -> None:
    registry, tree = tool_setup
    other_machine = insert_isolated_machine(temp_db)
    temp_db.execute("UPDATE sessions SET machine_id = %s WHERE id = %s", (other_machine, writer))

    result = await _set_note(registry, tree)

    assert result["success"] is False
    assert tree not in WorktreeNoteStore(temp_db).list_for([tree])
