"""Typed workspace ownership failures at the internal-tool boundary."""

from __future__ import annotations

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

pytestmark = pytest.mark.unit


def _raise_mismatch() -> None:
    raise MachineOwnershipMismatchError(
        resource_kind="worktree",
        resource_id="10000000-0000-4000-8000-000000000001",
        owner_machine_id="20000000-0000-4000-8000-000000000002",
        current_machine_id="20000000-0000-4000-8000-000000000001",
    )


@pytest.mark.asyncio
async def test_workspace_mismatch_serializes_consistently() -> None:
    registry = InternalToolRegistry(name="workspace-scope-test")

    @registry.tool(name="sync_mismatch", description="Raise from sync tool")
    def sync_mismatch() -> None:
        _raise_mismatch()

    @registry.tool(name="async_mismatch", description="Raise from async tool")
    async def async_mismatch() -> None:
        _raise_mismatch()

    expected = {
        "success": False,
        "error": (
            "Worktree 10000000-0000-4000-8000-000000000001 belongs to machine "
            "20000000-0000-4000-8000-000000000002, not current machine "
            "20000000-0000-4000-8000-000000000001"
        ),
        "error_code": "machine_ownership_mismatch",
        "resource_kind": "worktree",
        "resource_id": "10000000-0000-4000-8000-000000000001",
        "owner_machine_id": "20000000-0000-4000-8000-000000000002",
        "current_machine_id": "20000000-0000-4000-8000-000000000001",
    }

    assert await registry.call("sync_mismatch", {}) == expected
    assert await registry.call("async_mismatch", {}) == expected
    assert registry.call_sync("sync_mismatch", {}) == expected
