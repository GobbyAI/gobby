"""Hydration, child lifecycle, and dual-domain revision tests for agents."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.definitions import (
    AgentDefinitionManager,
    DefinitionNotFoundError,
    get_definitions_revision,
    register_revision_listener,
)
from gobby.storage.hub.postgres import PostgresHubDatabase

_PROJECT = str(uuid4())
_STEPS: dict[str, Any] = {
    "variables": {"required_skills": ["tdd"], "goal": "ship"},
    "exit_condition": "done",
    "steps": [
        {"name": "implement", "prompt": "write the code"},
        {"name": "review", "prompt": "check the diff"},
    ],
}


def _mgr(db: PostgresHubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


def _body(name: str = "coder", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"name": name, "role": "implementer", "provider": "inherit"}
    if extra:
        payload.update(extra)
    return payload


def _revisions(db: PostgresHubDatabase) -> tuple[int, int, int | None, int | None]:
    agents_local = get_definitions_revision("agents")
    child_local = get_definitions_revision("agent_step_workflows")
    agents_row = db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("agents",),
    )
    child_row = db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("agent_step_workflows",),
    )
    return (
        agents_local,
        child_local,
        None if agents_row is None else int(agents_row["revision"]),
        None if child_row is None else int(child_row["revision"]),
    )


def test_reads_hydrate_nested_step_workflow(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    created = manager.upsert_with_steps(
        "coder",
        _body("coder", extra={"steps": [{"name": "stale"}], "exit_condition": "old"}),
        _STEPS,
        description="impl",
        tags=["agent"],
    )

    stored = definition_db.fetchone(
        "SELECT definition_json FROM agent_definitions WHERE id = %s",
        (created.id,),
    )
    assert stored is not None
    assert "step_workflow" not in stored["definition_json"]
    assert "steps" not in stored["definition_json"]
    assert "exit_condition" not in stored["definition_json"]

    fetched = manager.get(created.id)
    assert fetched.step_workflow_id is not None
    assert fetched.definition_json["step_workflow"] == _STEPS
    by_name = manager.get_by_name("coder")
    assert by_name is not None
    assert by_name.definition_json["step_workflow"] == _STEPS
    listed = manager.list_all()
    assert [row.name for row in listed] == ["coder"]
    assert listed[0].definition_json["step_workflow"] == _STEPS
    assert listed[0].step_workflow_id == fetched.step_workflow_id


def test_upsert_with_steps_atomic(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    first = manager.upsert_with_steps("coder", _body("coder"), _STEPS)
    child = manager.get_step_workflow(first.id)
    assert child is not None
    assert child.steps_json == _STEPS["steps"]
    assert child.variables_json == _STEPS["variables"]
    assert child.exit_condition == _STEPS["exit_condition"]

    updated_steps = {
        "variables": {"required_skills": ["review"]},
        "exit_condition": "shipped",
        "steps": [{"name": "review", "prompt": "look again"}],
    }
    second = manager.upsert_with_steps(
        "coder",
        _body("coder", extra={"goal": "quality"}),
        updated_steps,
        description="updated",
    )
    assert second.id == first.id
    assert second.definition_json["goal"] == "quality"
    assert second.definition_json["step_workflow"] == updated_steps
    assert manager.get_step_workflow(first.id) is not None
    assert definition_db.fetchone(
        "SELECT count(*) AS n FROM agent_step_workflows WHERE agent_definition_id = %s",
        (first.id,),
    ) == {"n": 1}

    cleared = manager.upsert_with_steps("coder", _body("coder"), None)
    assert cleared.id == first.id
    assert "step_workflow" not in cleared.definition_json
    assert cleared.step_workflow_id is None
    assert manager.get_step_workflow(first.id) is None
    assert manager.get(first.id).name == "coder"

    before = _revisions(definition_db)
    with pytest.raises(RuntimeError, match="outer rollback"):
        with definition_db.transaction():
            manager.upsert_with_steps("ephemeral", _body("ephemeral"), _STEPS)
            raise RuntimeError("outer rollback")
    assert _revisions(definition_db) == before
    assert manager.get_by_name("ephemeral") is None
    assert definition_db.fetchone("SELECT count(*) AS n FROM agent_step_workflows") == {"n": 0}


def test_hard_delete_cascades_child(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    row = manager.upsert_with_steps("coder", _body(), _STEPS)
    child_id = row.step_workflow_id
    assert child_id is not None
    assert manager.hard_delete(row.id) is True
    assert manager.get_step_workflow(row.id) is None
    with pytest.raises(DefinitionNotFoundError):
        manager.get(row.id, include_deleted=True)
    leftover = definition_db.fetchone(
        "SELECT count(*) AS n FROM agent_step_workflows WHERE id = %s",
        (child_id,),
    )
    assert leftover == {"n": 0}


def test_soft_delete_restore_preserves_child(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    created = manager.upsert_with_steps("coder", _body(), _STEPS)
    child_before = manager.get_step_workflow(created.id)
    assert child_before is not None
    before_delete = _revisions(definition_db)

    assert manager.delete(created.id) is True
    after_delete = _revisions(definition_db)
    assert after_delete[0] == before_delete[0] + 1
    assert after_delete[1] == before_delete[1]
    assert after_delete[2] == (0 if before_delete[2] is None else before_delete[2]) + 1
    assert after_delete[3] == before_delete[3]

    child_while_deleted = manager.get_step_workflow(created.id)
    assert child_while_deleted is not None
    assert child_while_deleted.id == child_before.id
    assert child_while_deleted.steps_json == child_before.steps_json
    assert child_while_deleted.variables_json == child_before.variables_json
    assert child_while_deleted.exit_condition == child_before.exit_condition
    hidden = manager.get(created.id, include_deleted=True)
    assert hidden.definition_json["step_workflow"] == _STEPS

    restored = manager.restore(created.id)
    after_restore = _revisions(definition_db)
    assert after_restore[0] == after_delete[0] + 1
    assert after_restore[1] == after_delete[1]
    assert restored.step_workflow_id == child_before.id
    assert restored.definition_json["step_workflow"] == _STEPS
    child_after = manager.get_step_workflow(created.id)
    assert child_after is not None
    assert child_after.steps_json == child_before.steps_json
    assert child_after.variables_json == child_before.variables_json
    assert child_after.exit_condition == child_before.exit_condition


def test_child_write_bumps_both_domains_after_commit(
    definition_db: PostgresHubDatabase,
) -> None:
    manager = _mgr(definition_db)
    parent = manager.create(name="coder", definition_json=_body())
    before = _revisions(definition_db)
    fired: list[str] = []
    register_revision_listener("agents", lambda: fired.append("agents"))
    register_revision_listener("agent_step_workflows", lambda: fired.append("agent_step_workflows"))

    created = manager.set_step_workflow(parent.id, _STEPS)
    after_create = _revisions(definition_db)
    assert created.definition_json["step_workflow"] == _STEPS
    assert after_create[0] == before[0] + 1
    assert after_create[1] == before[1] + 1
    assert fired == ["agents", "agent_step_workflows"]

    fired.clear()
    manager.set_step_workflow(
        parent.id,
        {"variables": {}, "exit_condition": None, "steps": [{"name": "only"}]},
    )
    after_update = _revisions(definition_db)
    assert after_update[0] == after_create[0] + 1
    assert after_update[1] == after_create[1] + 1
    assert fired == ["agents", "agent_step_workflows"]

    fired.clear()
    cleared = manager.set_step_workflow(parent.id, None)
    after_delete = _revisions(definition_db)
    assert cleared.step_workflow_id is None
    assert after_delete[0] == after_update[0] + 1
    assert after_delete[1] == after_update[1] + 1
    assert fired == ["agents", "agent_step_workflows"]

    fired.clear()
    before_hard = _revisions(definition_db)
    manager.set_step_workflow(parent.id, _STEPS)
    manager.hard_delete(parent.id)
    after_hard = _revisions(definition_db)
    assert after_hard[0] == before_hard[0] + 2
    assert after_hard[1] == before_hard[1] + 2


def test_rolled_back_child_write_bumps_neither(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    parent = manager.create(name="coder", definition_json=_body())
    before = _revisions(definition_db)
    fired: list[str] = []
    register_revision_listener("agents", lambda: fired.append("agents"))
    register_revision_listener("agent_step_workflows", lambda: fired.append("agent_step_workflows"))

    with pytest.raises(RuntimeError, match="outer rollback"):
        with definition_db.transaction():
            manager.set_step_workflow(parent.id, _STEPS)
            raise RuntimeError("outer rollback")

    assert _revisions(definition_db) == before
    assert fired == []
    assert manager.get_step_workflow(parent.id) is None
    assert "step_workflow" not in manager.get(parent.id).definition_json


def test_purge_deleted_cascades_child_and_bumps_both(
    definition_db: PostgresHubDatabase,
) -> None:
    manager = _mgr(definition_db)
    row = manager.upsert_with_steps("coder", _body(), _STEPS)
    manager.delete(row.id)
    definition_db.execute(
        "UPDATE agent_definitions SET deleted_at = NOW() - INTERVAL '40 days' WHERE id = %s",
        (row.id,),
    )
    before = _revisions(definition_db)
    assert manager.purge_deleted(older_than_days=30) == 1
    after = _revisions(definition_db)
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 1
    assert definition_db.fetchone("SELECT count(*) AS n FROM agent_step_workflows") == {"n": 0}
