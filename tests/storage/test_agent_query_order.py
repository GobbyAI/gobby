"""Active-seat pagination retains every run when start times tie."""

from typing import Any
from uuid import UUID

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def test_active_run_pages_have_a_unique_tie_breaker(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    parent = AgentSessions(temp_db, sample_project["id"]).add("run-parent")
    runs = LocalAgentRunManager(temp_db)
    expected = [str(UUID(int=value)) for value in range(1, 5)]
    for identity in reversed(expected):
        run = runs.create(parent, "codex", "Hold a seat", agent_name="arbitrary-seat")
        temp_db.execute(
            "UPDATE agent_runs SET id = %s, started_at = NULL WHERE id = %s", (identity, run.id)
        )
    first = runs.list_active_for_machine(require_machine_id(), limit=2)
    second = runs.list_active_for_machine(require_machine_id(), limit=2, offset=2)
    assert [run.id for run in [*first, *second]] == expected
