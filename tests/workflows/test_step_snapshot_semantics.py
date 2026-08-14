"""Snapshot persistence, persona transitions, and fail-closed spawn semantics."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

pytestmark = pytest.mark.unit

S1 = "11111111-1111-4111-8111-111111111111"
LINEAGE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

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
CREATE TABLE IF NOT EXISTS session_variables (
    session_id uuid PRIMARY KEY,
    variables jsonb DEFAULT '{}'::jsonb,
    updated_at timestamptz DEFAULT now() NOT NULL
);
"""


@pytest.fixture
def snap_db(postgres_database_url: str) -> Iterator[PostgresHubDatabase]:
    schema = f"gobby_test_snap_{uuid4().hex[:12]}"
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


def _agent(
    name: str, steps: list[str], variables: dict[str, Any] | None = None
) -> AgentDefinitionBody:
    return AgentDefinitionBody(
        name=name,
        surfaces=["persona", "spawn"],
        step_workflow=AgentStepWorkflowBody(
            variables=dict(variables or {}),
            exit_condition=f"{name}_done",
            steps=[WorkflowStep(name=step) for step in steps],
        ),
    )


def _stepless(name: str) -> AgentDefinitionBody:
    return AgentDefinitionBody(name=name, surfaces=["persona"])


def _row_variables(row: Any) -> dict[str, Any]:
    payload: Any = row["variables"] if isinstance(row, Mapping) else row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


@pytest.mark.asyncio
async def test_persona_switch_replaces_or_deletes_instance(snap_db: PostgresHubDatabase) -> None:
    from gobby.mcp_proxy.tools.apply_persona import _apply_persona_instance_transition

    manager = AgentStepInstanceManager(snap_db)
    first = build_step_instance(
        _agent("alpha", ["claim", "implement"]), session_id=S1, step_workflow_id=LINEAGE
    )
    first.current_step = "implement"
    manager.save(first)

    _apply_persona_instance_transition(
        snap_db, S1, _agent("beta", ["review"]), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
    swapped = manager.get_for_session(S1)
    assert swapped is not None
    assert swapped.agent_name == "beta"
    assert swapped.current_step == "review"
    assert swapped.snapshot.steps[0].name == "review"

    _apply_persona_instance_transition(snap_db, S1, _stepless("comms"), None)
    assert manager.get_for_session(S1) is None


@pytest.mark.asyncio
async def test_persona_same_agent_missing_instance_creates_snapshot(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import _apply_persona_instance_transition

    manager = AgentStepInstanceManager(snap_db)
    assert manager.get_for_session(S1) is None
    _apply_persona_instance_transition(snap_db, S1, _agent("alpha", ["claim"]), LINEAGE)
    created = manager.get_for_session(S1)
    assert created is not None
    assert created.agent_name == "alpha"
    assert created.current_step == "claim"


@pytest.mark.asyncio
async def test_persona_switch_is_atomic_across_rows(snap_db: PostgresHubDatabase) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
    from gobby.workflows.state_manager import SessionVariableManager

    manager = AgentStepInstanceManager(snap_db)
    manager.save(
        build_step_instance(_agent("alpha", ["claim"]), session_id=S1, step_workflow_id=LINEAGE)
    )
    SessionVariableManager(snap_db).merge_variables(S1, {"_agent_type": "alpha"})

    beta = _agent("beta", ["review"])
    row = MagicMock()
    row.step_workflow_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(beta, row),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
            return_value=({"_agent_type": "beta"}, set()),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona._resolve_session_identity",
            return_value=(None, "claude"),
        ),
        patch.object(
            SessionVariableManager,
            "merge_variables",
            side_effect=RuntimeError("variable merge failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="variable merge failed"):
            await apply_persona_impl(agent="beta", db=snap_db, session_id=S1)
    remaining = manager.get_for_session(S1)
    assert remaining is not None
    assert remaining.agent_name == "alpha"
    stored = snap_db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s", (S1,)
    )
    assert stored is not None
    assert _row_variables(stored)["_agent_type"] == "alpha"


@pytest.mark.asyncio
async def test_apply_persona_rejects_reserved_caller_variables(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
    from gobby.workflows.state_manager import SessionVariableManager

    manager = AgentStepInstanceManager(snap_db)
    manager.save(
        build_step_instance(_agent("alpha", ["claim"]), session_id=S1, step_workflow_id=LINEAGE)
    )
    SessionVariableManager(snap_db).merge_variables(S1, {"_agent_type": "alpha", "ok": 1})
    before = manager.get_for_session(S1)
    assert before is not None

    beta = _agent("beta", ["review"])
    row = MagicMock()
    row.step_workflow_id = LINEAGE
    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(beta, row),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
            return_value=({"_agent_type": "beta"}, set()),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona._resolve_session_identity",
            return_value=(None, "claude"),
        ),
    ):
        for payload in (
            {"_agent_type": "smuggled"},
            {"step_workflow_complete": True},
        ):
            result = await apply_persona_impl(
                agent="beta",
                db=snap_db,
                session_id=S1,
                variables=payload,
            )
            assert result["success"] is False
            current = manager.get_for_session(S1)
            assert current is not None
            assert current.agent_name == "alpha"
            stored = snap_db.fetchone(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (S1,),
            )
            assert stored is not None
            assert _row_variables(stored)["_agent_type"] == "alpha"

        result = await apply_persona_impl(
            agent="beta",
            db=snap_db,
            session_id=S1,
            variables={"note": "keep"},
        )
    assert result["success"] is True
    after = manager.get_for_session(S1)
    assert after is not None
    assert after.agent_name == "beta"
    stored = snap_db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s", (S1,)
    )
    assert stored is not None
    assert _row_variables(stored)["note"] == "keep"


def test_failed_instance_save_aborts_before_launch() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance

    db = MagicMock()
    manager = MagicMock()
    manager.save.side_effect = RuntimeError("save failed")
    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._step_state.AgentStepInstanceManager",
            return_value=manager,
        ),
        pytest.raises(RuntimeError, match="save failed"),
    ):
        persist_initial_step_instance(
            db,
            _agent("alpha", ["claim"]),
            session_id=S1,
            step_workflow_id=LINEAGE,
        )


@pytest.mark.asyncio
async def test_prelaunch_faults_leave_no_rows() -> None:
    from gobby.agents.spawn import cleanup_unlaunched_spawn

    session_manager = MagicMock()
    session_manager._storage.db.execute.side_effect = [None, None]
    session_manager._storage.delete.return_value = True
    cleanup_unlaunched_spawn(
        session_manager,
        session_id=S1,
        agent_run_id="22222222-2222-4222-8222-222222222222",
        prompt_file=None,
        managed_credential=None,
    )
    cleanup_unlaunched_spawn(
        session_manager,
        session_id=S1,
        agent_run_id="22222222-2222-4222-8222-222222222222",
        prompt_file=None,
        managed_credential=None,
    )
    assert session_manager._storage.delete.call_count == 2


@pytest.mark.asyncio
async def test_post_launch_faults_leave_no_live_process() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import cleanup_failed_spawn

    runner = MagicMock()
    runner.run_storage.get.return_value = MagicMock(child_session_id=S1, pid=4242)
    runner.child_session_manager._storage.delete.return_value = True
    with (
        patch("os.kill") as kill,
        patch(
            "gobby.agents.tmux.get_tmux_session_manager",
        ) as tmux_factory,
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            new_callable=AsyncMock,
        ),
    ):
        tmux = MagicMock()
        tmux.kill_session = AsyncMock(return_value=True)
        tmux_factory.return_value = tmux
        await cleanup_failed_spawn(
            runner,
            "run-1",
            "lease attach failed",
            handler=MagicMock(),
            spawn_config=MagicMock(),
            completion_registry=None,
            cleanup_isolation=False,
            task_manager=None,
            child_session_id=S1,
            pid=4242,
            tmux_session_name="gobby-run",
        )
    assert kill.called
    tmux.kill_session.assert_awaited()


def test_auto_claimed_spawn_initial_step_preserved(snap_db: PostgresHubDatabase) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._step_state import (
        apply_claimed_step_update,
        persist_initial_step_instance,
    )

    agent = _agent("coder", ["claim", "implement"], {"goal": "ship"})
    persist_initial_step_instance(
        snap_db,
        agent,
        session_id=S1,
        step_workflow_id=LINEAGE,
        task_owned_by_child=False,
    )
    before = AgentStepInstanceManager(snap_db).get_for_session(S1)
    assert before is not None
    assert before.current_step == "claim"
    assert before.variables.get("task_claimed") is not True

    apply_claimed_step_update(snap_db, agent, session_id=S1)
    after = AgentStepInstanceManager(snap_db).get_for_session(S1)
    assert after is not None
    assert after.variables.get("task_claimed") is True
    assert after.current_step in {"claim", "implement"}
    assert after.snapshot.model_dump() == before.snapshot.model_dump()


def test_dispatch_spawn_does_not_seed_step_workflow_name() -> None:
    from pathlib import Path

    source = Path("src/gobby/dispatch/spawn.py").read_text(encoding="utf-8")
    assert "_step_workflow_name" not in source
    assert "_register_agent_step_workflow" not in source


def test_fresh_snapshot_recovery_emits_structured_warning(
    snap_db: PostgresHubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    from types import SimpleNamespace

    from gobby.hooks.session_activation import (
        FRESH_SNAPSHOT_RECOVERY_MARKER,
        _ensure_step_instance,
    )

    agent = _agent("alpha", ["claim", "implement"], {"goal": "ship"})
    row = MagicMock()
    row.id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    row.step_workflow_id = LINEAGE
    session = SimpleNamespace(
        project_id=None,
        parent_session_id=None,
        agent_run_id=None,
        agent_depth=0,
    )
    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(agent, row),
        ),
        caplog.at_level(logging.WARNING, logger="gobby.hooks.session_activation"),
    ):
        created = _ensure_step_instance(snap_db, S1, {"_agent_type": "alpha"}, session)
    assert created is True
    recovered = AgentStepInstanceManager(snap_db).get_for_session(S1)
    assert recovered is not None
    assert recovered.agent_name == "alpha"
    assert recovered.current_step == "claim"
    assert recovered.variables["goal"] == "ship"
    assert recovered.agent_step_workflow_id == LINEAGE
    assert caplog.text.count(FRESH_SNAPSHOT_RECOVERY_MARKER) == 1
    assert S1 in caplog.text
    assert "alpha" in caplog.text
    assert row.id in caplog.text
    assert LINEAGE in caplog.text

    with patch(
        "gobby.workflows.agent_resolver.resolve_agent_with_row",
        return_value=(agent, row),
    ):
        assert _ensure_step_instance(snap_db, S1, {"_agent_type": "alpha"}, session) is False
    assert caplog.text.count(FRESH_SNAPSHOT_RECOVERY_MARKER) == 1


def test_compacted_mid_workflow_resume_keeps_step_and_variables(
    snap_db: PostgresHubDatabase,
) -> None:
    manager = AgentStepInstanceManager(snap_db)
    instance = build_step_instance(
        _agent("alpha", ["claim", "implement"], {"goal": "ship"}),
        session_id=S1,
        step_workflow_id=LINEAGE,
        current_step="implement",
        variables={"goal": "ship", "progress": 2},
    )
    manager.save(instance)

    resumed = manager.get_for_session(S1)
    assert resumed is not None
    assert resumed.session_id == S1
    assert resumed.current_step == "implement"
    assert resumed.variables == {"goal": "ship", "progress": 2}


def test_workflow_instance_types_are_gone() -> None:
    import gobby.workflows.definitions as definitions
    import gobby.workflows.state_manager as state_manager

    assert not hasattr(state_manager, "WorkflowInstanceManager")
    assert not hasattr(definitions, "WorkflowInstance")


def test_reserved_variables_omit_step_workflow_name() -> None:
    from gobby.workflows.reserved_variables import (
        RESERVED_WORKFLOW_VARIABLES,
        is_reserved_workflow_variable,
    )

    assert "_step_workflow_name" not in RESERVED_WORKFLOW_VARIABLES
    assert is_reserved_workflow_variable("step_workflow_complete")
    assert not is_reserved_workflow_variable("_step_workflow_name")


def test_enforcement_write_paths_hold_one_critical_section() -> None:
    from gobby.storage.hub.protocol import AgentStepInstanceMutation
    from gobby.workflows.engine.enforcement_checks import EnforcementCheckMixin

    class _Engine(EnforcementCheckMixin):
        def __init__(self) -> None:
            self.db = MagicMock()
            self.instance_manager = MagicMock()
            self.instance_manager.get_for_session.return_value = None

    engine = _Engine()
    event = MagicMock()
    event.data = {"tool_name": "Read", "tool_input": {}}
    engine._check_step_tool_enforcement(event, S1, {})
    engine.db.transaction_immediate.assert_called_once()
    lock = engine.db.transaction_immediate.call_args[0][0]
    assert isinstance(lock, AgentStepInstanceMutation)
    assert lock.session_id == S1
