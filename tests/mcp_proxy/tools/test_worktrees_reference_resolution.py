"""gobby-worktrees tools resolve ``worktree_id`` refs before touching storage (#21521).

``merge_worktree`` with an 8-char prefix used to return psycopg's
``invalid input syntax for type uuid``. Every tool that takes a
``worktree_id`` now goes through ``RegistryContext.resolve_worktree_id`` first,
so prefixes act on the matching worktree and unmatched refs fail cleanly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

_SEEDED_TEST_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
_PREFIX = "0b0b0b0b"
_FULL_ID = f"{_PREFIX}-3333-4333-8333-333333333333"


@pytest.fixture
def storage(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LocalWorktreeManager:
    """One worktree on an isolated project whose machine is pinned as local."""
    project = install_isolated_checkout_project(
        temp_db,
        tmp_path / "isolated-checkout",
        machine_id=_SEEDED_TEST_MACHINE_ID,
        monkeypatch=monkeypatch,
    ).project
    manager = LocalWorktreeManager(temp_db)
    created = manager.create(
        project_id=project.id,
        branch_name="feature/prefix-target",
        worktree_path="/tmp/worktrees/prefix-target",
        agent_session_id=None,
    )
    temp_db.execute("UPDATE worktrees SET id = %s WHERE id = %s", (_FULL_ID, created.id))
    return manager


class TestPrefixRefsActOnTheMatchingWorktree:
    @pytest.mark.asyncio
    async def test_get_worktree_accepts_a_prefix(self, storage: LocalWorktreeManager) -> None:
        registry = create_worktrees_registry(worktree_storage=storage)

        result = await registry.call("get_worktree", {"worktree_id": _PREFIX})

        assert result["success"] is True, result
        assert result["worktree"]["id"] == _FULL_ID

    @pytest.mark.asyncio
    async def test_release_worktree_accepts_a_prefix(
        self, storage: LocalWorktreeManager, temp_db: HubDatabase
    ) -> None:
        temp_db.execute(
            "UPDATE worktrees SET agent_session_id = NULL WHERE id = %s",
            (_FULL_ID,),
        )
        registry = create_worktrees_registry(worktree_storage=storage)

        result = await registry.call("release_worktree", {"worktree_id": _PREFIX})

        assert result["success"] is True, result
        assert result["event"]["worktree_id"] == _FULL_ID

    @pytest.mark.asyncio
    async def test_abandon_worktree_accepts_a_prefix(self, storage: LocalWorktreeManager) -> None:
        registry = create_worktrees_registry(worktree_storage=storage)

        result = await registry.call("abandon_worktree", {"worktree_id": _PREFIX})

        assert result["success"] is True, result
        abandoned = storage.get(_FULL_ID)
        assert abandoned is not None
        assert abandoned.status == WorktreeStatus.ABANDONED.value

    @pytest.mark.asyncio
    async def test_unknown_prefix_is_a_clean_not_found(self, storage: LocalWorktreeManager) -> None:
        registry = create_worktrees_registry(worktree_storage=storage)

        result = await registry.call("get_worktree", {"worktree_id": "deadbeef"})

        assert result["success"] is False
        assert "not found" in result["error"]
        assert "invalid input syntax" not in result["error"]


def _prefix_only_storage() -> MagicMock:
    """Storage whose resolver is the only method that understands the prefix."""
    stub = MagicMock(spec=LocalWorktreeManager)
    stub.resolve_reference = MagicMock(return_value=_FULL_ID)
    stub.get = MagicMock(return_value=None)
    stub.claim_if_available = MagicMock(return_value=None)
    return stub


_TOOL_ARGUMENTS: list[tuple[str, dict[str, Any]]] = [
    ("get_worktree", {}),
    ("claim_worktree", {"session_id": "11111111-1111-4111-8111-111111111111"}),
    ("release_worktree", {}),
    ("delete_worktree", {}),
    ("mark_worktree_merged", {}),
    ("abandon_worktree", {}),
    ("reactivate_worktree", {}),
    ("link_task_to_worktree", {"task_id": "#1"}),
    ("sync_worktree", {}),
    ("merge_worktree", {}),
    ("push_branch", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"), _TOOL_ARGUMENTS, ids=[tool for tool, _ in _TOOL_ARGUMENTS]
)
async def test_every_worktree_id_tool_resolves_before_storage_lookup(
    tool: str, arguments: dict[str, Any]
) -> None:
    stub = _prefix_only_storage()
    registry = create_worktrees_registry(
        worktree_storage=stub,
        git_manager=MagicMock(),
        project_id="project-a",
        worktree_delete_executor=None,
    )

    await registry.call(tool, {"worktree_id": _PREFIX, **arguments})

    stub.resolve_reference.assert_called_once_with(_PREFIX)
    lookups = [call.args[0] for call in stub.get.call_args_list]
    lookups += [call.args[0] for call in stub.claim_if_available.call_args_list]
    assert lookups, f"{tool} never consulted storage"
    assert all(lookup == _FULL_ID for lookup in lookups), lookups


@pytest.mark.asyncio
async def test_resolver_error_is_returned_verbatim() -> None:
    stub = _prefix_only_storage()
    stub.resolve_reference.side_effect = ValueError("Ambiguous worktree '0b' matches: a, b")
    registry = create_worktrees_registry(worktree_storage=stub)

    result = await registry.call("get_worktree", {"worktree_id": "0b"})

    assert result == {"success": False, "error": "Ambiguous worktree '0b' matches: a, b"}
    stub.get.assert_not_called()
