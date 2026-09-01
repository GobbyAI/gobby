from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.worktrees import LocalWorktreeManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

_SEEDED_TEST_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_test_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the seeded test machine id as local for ownership guards."""
    monkeypatch.setattr(
        "gobby.utils.machine_id.get_machine_id",
        lambda: _SEEDED_TEST_MACHINE_ID,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("session_ref_kind", ["hash", "uuid"], ids=["hash-ref", "uuid-ref"])
async def test_claim_worktree_resolves_session_refs(
    temp_db, session_manager, session_ref_kind: str, tmp_path: Path
) -> None:
    """claim_worktree resolves shorthand and UUID session references before claiming."""
    worktree_storage = LocalWorktreeManager(temp_db)
    project = install_isolated_checkout_project(
        temp_db, tmp_path / "isolated-checkout", machine_id=_SEEDED_TEST_MACHINE_ID
    ).project
    session = session_manager.register(
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
        external_id="ext-test-session",
        title="Test Session",
    )
    worktree = worktree_storage.create(
        project_id=project.id,
        branch_name=f"feature/{session_ref_kind}",
        worktree_path=f"/tmp/worktrees/{session_ref_kind}",
    )
    registry = create_worktrees_registry(
        worktree_storage=worktree_storage,
        project_id=project.id,
        session_manager=session_manager,
    )

    session_ref = f"#{session.seq_num}" if session_ref_kind == "hash" else session.id
    result = await registry.call(
        "claim_worktree",
        {"worktree_id": worktree.id, "session_id": session_ref},
    )

    assert result["success"] is True
    claimed = worktree_storage.get(worktree.id)
    assert claimed is not None
    assert claimed.agent_session_id == session.id
