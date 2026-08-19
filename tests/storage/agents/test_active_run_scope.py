"""Machine-scope contracts for active agent-run readers."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import gobby.storage.agents._lifecycle as lifecycle_module
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


def test_local_and_global_active_run_apis(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local readers filter by machine while global invariants retain both rows."""
    # Session registration rejects foreign machine identity, so the parent
    # session must live on the real current machine; only the remote agent
    # run carries a foreign machine id.
    local_machine_id = require_machine_id()
    remote_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, remote_machine_id):
        temp_db.execute(
            "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (machine_id, f"host-{machine_id}", TEST_USER_ID),
        )

    parent = session_manager.register(
        external_id=f"active-scope-{uuid.uuid4()}",
        machine_id=local_machine_id,
        source="codex",
        project_id=sample_project["id"],
    )
    owners = iter((local_machine_id, remote_machine_id))
    monkeypatch.setattr(lifecycle_module, "get_machine_id", lambda: next(owners))
    manager = LocalAgentRunManager(temp_db)
    local_run = manager.create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="local",
    )
    remote_run = manager.create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="remote",
    )
    manager.start(local_run.id)
    manager.start(remote_run.id)

    assert [run.id for run in manager.list_active_for_machine(local_machine_id)] == [local_run.id]
    assert {run.id for run in manager.list_active_global()} == {local_run.id, remote_run.id}
    assert not hasattr(manager, "list_active")
