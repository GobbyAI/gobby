"""Shared PreparedSpawn fixture for spawn-request constructors."""

from __future__ import annotations

from typing import Any

from gobby.agents.spawn import PreparedSpawn


def prepared_spawn(**overrides: Any) -> PreparedSpawn:
    """Return a required prepared-spawn object for SpawnRequest constructors."""
    values: dict[str, Any] = {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "agent_run_id": "22222222-2222-4222-8222-222222222222",
        "parent_session_id": "parent",
        "project_id": "proj",
        "workflow_name": None,
        "agent_depth": 1,
        "env_vars": {},
    }
    values.update(overrides)
    return PreparedSpawn(**values)
