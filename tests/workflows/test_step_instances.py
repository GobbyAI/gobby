"""AgentStepInstance manager: immutable snapshots, replace, locks, CAS."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep

pytestmark = pytest.mark.unit

_INSTANCE_SQL = """
CREATE TABLE IF NOT EXISTS agent_step_instances (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL UNIQUE,
    agent_step_workflow_id uuid,
    agent_name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    current_step text,
    step_entered_at timestamptz,
    step_action_count integer DEFAULT 0 NOT NULL,
    total_action_count integer DEFAULT 0 NOT NULL,
    variables jsonb DEFAULT '{}'::jsonb NOT NULL,
    context_injected boolean DEFAULT false NOT NULL,
    snapshot_json jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
"""

S1 = "11111111-1111-4111-8111-111111111111"
LINEAGE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LINEAGE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture
def instance_db(postgres_database_url: str) -> Iterator[PostgresHubDatabase]:
    schema = f"gobby_test_asi_{uuid4().hex[:12]}"
    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(_INSTANCE_SQL)
    database = PostgresHubDatabase(f"{postgres_database_url}?options=-csearch_path%3D{schema}")
    try:
        yield database
    finally:
        database.close()
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _workflow(
    name: str, steps: list[str], variables: dict[str, Any] | None = None
) -> AgentStepWorkflowBody:
    return AgentStepWorkflowBody(
        variables=dict(variables or {}),
        exit_condition=f"{name}_done",
        steps=[WorkflowStep(name=step) for step in steps],
    )


def _agent(
    name: str, steps: list[str], variables: dict[str, Any] | None = None
) -> AgentDefinitionBody:
    return AgentDefinitionBody(name=name, step_workflow=_workflow(name, steps, variables))


def _require_step_workflow(agent: AgentDefinitionBody) -> AgentStepWorkflowBody:
    assert agent.step_workflow is not None
    return agent.step_workflow


def _mgr(db: PostgresHubDatabase) -> Any:
    from gobby.workflows.step_instances import AgentStepInstanceManager

    return AgentStepInstanceManager(db)


def test_one_instance_per_session_upsert(instance_db: PostgresHubDatabase) -> None:
    from gobby.workflows.step_instances import build_step_instance

    manager = _mgr(instance_db)
    agent = _agent("coder", ["implement", "review"], {"goal": "ship"})
    built = build_step_instance(agent, session_id=S1, step_workflow_id=LINEAGE_A)
    assert built.agent_name == "coder"
    assert built.current_step == "implement"
    assert built.variables == {"goal": "ship"}
    assert built.snapshot.model_dump() == _require_step_workflow(agent).model_dump()

    manager.save(built)
    first = manager.get_for_session(S1)
    assert first is not None
    assert first.session_id == S1
    assert first.agent_step_workflow_id == LINEAGE_A
    first_id = first.id
    first_created = first.created_at

    first.current_step = "review"
    first.variables = {"goal": "ship", "done": True}
    first.step_action_count = 3
    manager.save(first)

    again = manager.get_for_session(S1)
    assert again is not None
    assert again.id == first_id
    assert again.created_at == first_created
    assert again.current_step == "review"
    assert again.variables == {"goal": "ship", "done": True}
    assert again.step_action_count == 3
    assert again.agent_name == "coder"
    assert again.snapshot.model_dump() == _require_step_workflow(agent).model_dump()


def test_snapshot_immutable_on_upsert(instance_db: PostgresHubDatabase) -> None:
    from gobby.workflows.step_instances import AgentStepInstance, build_step_instance

    manager = _mgr(instance_db)
    original_body = _agent("coder", ["implement"], {"v": 1})
    instance = build_step_instance(original_body, session_id=S1, step_workflow_id=LINEAGE_A)
    manager.save(instance)
    stored = manager.get_for_session(S1)
    assert stored is not None
    created_at = stored.created_at
    original_dump = stored.snapshot.model_dump()

    mutated = AgentStepInstance(
        id=str(uuid4()),
        session_id=S1,
        agent_name="coder",
        agent_step_workflow_id=LINEAGE_B,
        snapshot=_workflow("other", ["hack"], {"v": 99}),
        current_step="hack",
        variables={"v": 99},
        created_at=created_at - timedelta(days=1),
        updated_at=datetime.now(UTC),
    )
    manager.save(mutated)

    after = manager.get_for_session(S1)
    assert after is not None
    assert after.snapshot.model_dump() == original_dump
    assert after.agent_step_workflow_id == LINEAGE_A
    assert after.created_at == created_at
    assert after.current_step == "hack"
    assert after.variables == {"v": 99}


def test_agent_step_instance_mutation_replaces_workflow_lock() -> None:
    from gobby.storage.hub import protocol

    assert hasattr(protocol, "AgentStepInstanceMutation")
    assert not hasattr(protocol, "WorkflowInstanceMutation")
    assert "AgentStepInstanceMutation" in protocol.__all__
    assert "WorkflowInstanceMutation" not in protocol.__all__
    lock = protocol.AgentStepInstanceMutation(session_id=S1)
    assert lock.session_id == S1
    assert not hasattr(lock, "workflow_name")
    fields = protocol.AgentStepInstanceMutation.__dataclass_fields__
    assert "session_id" in fields
    assert "workflow_name" not in fields


def test_replace_for_session_swaps_snapshot_and_lineage(instance_db: PostgresHubDatabase) -> None:
    from gobby.workflows.step_instances import build_step_instance

    manager = _mgr(instance_db)
    first = build_step_instance(
        _agent("coder", ["implement"], {"owner": "coder"}),
        session_id=S1,
        step_workflow_id=LINEAGE_A,
        current_step="implement",
    )
    manager.save(first)
    original = manager.get_for_session(S1)
    assert original is not None

    replacement = build_step_instance(
        _agent("reviewer", ["audit", "ship"], {"owner": "reviewer"}),
        session_id=S1,
        step_workflow_id=LINEAGE_B,
        current_step="audit",
        variables={"owner": "reviewer", "switched": True},
    )
    manager.replace_for_session(replacement)

    stored = manager.get_for_session(S1)
    assert stored is not None
    assert stored.agent_name == "reviewer"
    assert stored.agent_step_workflow_id == LINEAGE_B
    assert stored.current_step == "audit"
    assert stored.snapshot.model_dump() == replacement.snapshot.model_dump()
    assert stored.variables == {"owner": "reviewer", "switched": True}
    assert stored.created_at != original.created_at or stored.id != original.id

    stored.current_step = "ship"
    stored.variables = {"owner": "reviewer", "switched": True, "n": 1}
    manager.save(stored)
    after_save = manager.get_for_session(S1)
    assert after_save is not None
    assert after_save.agent_name == "reviewer"
    assert after_save.agent_step_workflow_id == LINEAGE_B
    assert after_save.snapshot.model_dump() == replacement.snapshot.model_dump()
    assert after_save.current_step == "ship"

    merged = manager.merge_variables(S1, {"extra": True})
    assert merged is not None
    assert merged.agent_name == "reviewer"
    assert merged.agent_step_workflow_id == LINEAGE_B
    assert merged.snapshot.model_dump() == replacement.snapshot.model_dump()


def test_merge_variables_serializes_against_save(instance_db: PostgresHubDatabase) -> None:
    from gobby.storage.hub.protocol import AgentStepInstanceMutation
    from gobby.workflows.step_instances import build_step_instance

    manager = _mgr(instance_db)
    manager.save(
        build_step_instance(
            _agent("coder", ["implement", "review"], {"existing": "value"}),
            session_id=S1,
            step_workflow_id=LINEAGE_A,
        )
    )
    transition_read = threading.Event()
    variable_write_started = threading.Event()
    finish_transition = threading.Event()

    def transition_step() -> None:
        lock = AgentStepInstanceMutation(session_id=S1)
        with instance_db.transaction_immediate(lock):
            instance = manager.get_for_session(S1)
            assert instance is not None
            transition_read.set()
            assert finish_transition.wait(timeout=5)
            instance.current_step = "review"
            manager.save(instance)

    def merge() -> Any:
        assert transition_read.wait(timeout=5)
        variable_write_started.set()
        return manager.merge_variables(S1, {"new": "value"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        transition_future = executor.submit(transition_step)
        merge_future = executor.submit(merge)
        assert variable_write_started.wait(timeout=5)
        finish_transition.set()
        transition_future.result(timeout=5)
        merged = merge_future.result(timeout=5)

    assert merged is not None
    result = manager.get_for_session(S1)
    assert result is not None
    assert result.current_step == "review"
    assert result.variables == {"existing": "value", "new": "value"}


def test_mutation_lock_is_reentrant(instance_db: PostgresHubDatabase) -> None:
    from gobby.storage.hub.protocol import AgentStepInstanceMutation
    from gobby.workflows.step_instances import build_step_instance

    manager = _mgr(instance_db)
    manager.save(
        build_step_instance(
            _agent("coder", ["implement", "review"], {"existing": "value"}),
            session_id=S1,
            step_workflow_id=LINEAGE_A,
        )
    )
    entered = threading.Event()
    merge_started = threading.Event()
    finish_section = threading.Event()

    def caller_span() -> None:
        lock = AgentStepInstanceMutation(session_id=S1)
        with instance_db.transaction_immediate(lock):
            instance = manager.get_for_session(S1)
            assert instance is not None
            entered.set()
            assert finish_section.wait(timeout=5)
            instance.current_step = "review"
            instance.step_action_count = 4
            manager.save(instance)

    def outsider_merge() -> Any:
        assert entered.wait(timeout=5)
        merge_started.set()
        return manager.merge_variables(S1, {"new": "value"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        span_future = executor.submit(caller_span)
        merge_future = executor.submit(outsider_merge)
        assert merge_started.wait(timeout=5)
        finish_section.set()
        span_future.result(timeout=5)
        merged = merge_future.result(timeout=5)

    assert merged is not None
    result = manager.get_for_session(S1)
    assert result is not None
    assert result.current_step == "review"
    assert result.step_action_count == 4
    assert result.variables == {"existing": "value", "new": "value"}


def test_stale_save_after_persona_replacement_rejected(instance_db: PostgresHubDatabase) -> None:
    from gobby.workflows.step_instances import StaleStepInstanceWriteError, build_step_instance

    manager = _mgr(instance_db)
    first = build_step_instance(
        _agent("coder", ["implement"], {"owner": "coder"}),
        session_id=S1,
        step_workflow_id=LINEAGE_A,
        current_step="implement",
    )
    manager.save(first)
    stale = manager.get_for_session(S1)
    assert stale is not None

    replacement = build_step_instance(
        _agent("reviewer", ["audit"], {"owner": "reviewer"}),
        session_id=S1,
        step_workflow_id=LINEAGE_B,
        current_step="audit",
        variables={"owner": "reviewer"},
    )
    manager.replace_for_session(replacement)
    replaced = manager.get_for_session(S1)
    assert replaced is not None

    stale.current_step = "should-not-land"
    stale.variables = {"owner": "coder", "leaked": True}
    with pytest.raises(StaleStepInstanceWriteError):
        manager.save(
            stale,
            if_match=(str(stale.id), stale.updated_at),
        )

    after = manager.get_for_session(S1)
    assert after is not None
    assert after.agent_name == "reviewer"
    assert after.agent_step_workflow_id == LINEAGE_B
    assert after.current_step == "audit"
    assert after.variables == {"owner": "reviewer"}
    assert after.snapshot.model_dump() == replacement.snapshot.model_dump()


def test_save_rejects_agent_identity_change(instance_db: PostgresHubDatabase) -> None:
    from gobby.workflows.step_instances import StaleStepInstanceWriteError, build_step_instance

    manager = _mgr(instance_db)
    manager.save(
        build_step_instance(
            _agent("coder", ["implement"], {"owner": "coder"}),
            session_id=S1,
            step_workflow_id=LINEAGE_A,
        )
    )
    stored = manager.get_for_session(S1)
    assert stored is not None
    original_dump = stored.snapshot.model_dump()

    stored.agent_name = "reviewer"
    stored.current_step = "nope"
    with pytest.raises(StaleStepInstanceWriteError):
        manager.save(stored)

    again = manager.get_for_session(S1)
    assert again is not None
    assert again.agent_name == "coder"
    assert again.current_step == "implement"
    assert again.snapshot.model_dump() == original_dump

    again.agent_name = "reviewer"
    with pytest.raises(StaleStepInstanceWriteError):
        manager.save(
            again,
            if_match=(str(again.id), again.updated_at),
        )

    final = manager.get_for_session(S1)
    assert final is not None
    assert final.agent_name == "coder"
    assert final.agent_step_workflow_id == LINEAGE_A
    assert final.snapshot.model_dump() == original_dump
    assert final.current_step == "implement"
