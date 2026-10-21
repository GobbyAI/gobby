"""The retained research stage resolves its bundled researcher definition."""

from __future__ import annotations

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._stage_registry_loader import StageRegistryLoader

pytestmark = pytest.mark.unit


def test_research_stage_default_agent_resolves(temp_db: HubDatabase) -> None:
    sync_bundled_agents(temp_db)
    StageRegistryLoader().sync(temp_db)

    rows = temp_db.fetchall(
        """
        SELECT r.name AS stage_name, r.default_agent, w.enabled
        FROM task_stages_registry r
        LEFT JOIN agent_definitions w
            ON w.name = r.default_agent
           AND w.deleted_at IS NULL
        WHERE r.name = 'research'
        """
    )

    assert {row["stage_name"]: row["default_agent"] for row in rows} == {"research": "researcher"}
    assert {row["stage_name"]: bool(row["enabled"]) for row in rows} == {"research": True}
