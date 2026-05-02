"""Red tests for discovery-stage default-agent resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit

DISCOVERY_DEFAULT_AGENTS = {
    "ideation": "analyst",
    "research": "researcher",
    "architecture": "architect",
    "prd": "product-manager",
}


def test_discovery_stage_default_agents_resolve(tmp_path: Path) -> None:
    from gobby.storage.tasks._stage_registry_loader import StageRegistryLoader  # noqa: PLC0415

    db = LocalDatabase(tmp_path / "default-agent-fk.db")
    run_migrations(db)
    sync_bundled_agents(db)
    StageRegistryLoader().sync(db)

    rows = db.fetchall(
        """
        SELECT r.name AS stage_name, r.default_agent, w.enabled
        FROM task_stages_registry r
        LEFT JOIN workflow_definitions w
            ON w.workflow_type = 'agent'
           AND w.name = r.default_agent
           AND w.deleted_at IS NULL
        WHERE r.name IN ('ideation', 'research', 'architecture', 'prd')
        ORDER BY r.position_hint
        """
    )

    assert {row["stage_name"]: row["default_agent"] for row in rows} == (
        DISCOVERY_DEFAULT_AGENTS
    )
    assert {row["stage_name"]: bool(row["enabled"]) for row in rows} == dict.fromkeys(
        DISCOVERY_DEFAULT_AGENTS, False
    )
