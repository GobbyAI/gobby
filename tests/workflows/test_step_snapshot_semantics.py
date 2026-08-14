"""Snapshot persistence, persona transitions, and fail-closed spawn semantics."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
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
S2 = "22222222-2222-4222-8222-222222222222"
LINEAGE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"

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

_TYPED_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id uuid PRIMARY KEY,
    project_id uuid
);
CREATE TABLE IF NOT EXISTS session_variable_defaults (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    default_value jsonb,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS definition_revisions (
    domain text PRIMARY KEY,
    revision bigint DEFAULT 0 NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_defs_live_name
    ON agent_definitions USING btree (name, project_id) NULLS NOT DISTINCT
    WHERE (deleted_at IS NULL);
CREATE TABLE IF NOT EXISTS agent_step_workflows (
    id uuid PRIMARY KEY,
    agent_definition_id uuid NOT NULL UNIQUE
        REFERENCES agent_definitions(id) ON DELETE CASCADE,
    steps_json jsonb NOT NULL,
    variables_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    exit_condition text,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_step_instances (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL UNIQUE,
    agent_step_workflow_id uuid
        REFERENCES agent_step_workflows(id) ON DELETE SET NULL,
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
CREATE TABLE IF NOT EXISTS workflow_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    workflow_type text DEFAULT 'workflow'::text NOT NULL,
    version text DEFAULT '1.0'::text,
    enabled boolean DEFAULT true,
    enabled_user_modified boolean DEFAULT false NOT NULL,
    priority integer DEFAULT 100,
    sources jsonb,
    definition_json jsonb NOT NULL,
    canvas_json jsonb,
    source text DEFAULT 'installed'::text,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
"""


def _schema_db(postgres_database_url: str, ddl: str) -> Iterator[PostgresHubDatabase]:
    schema = f"gobby_test_snap_{uuid4().hex[:12]}"
    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(ddl)
    database = PostgresHubDatabase(f"{postgres_database_url}?options=-csearch_path%3D{schema}")
    try:
        yield database
    finally:
        database.close()
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def snap_db(postgres_database_url: str) -> Iterator[PostgresHubDatabase]:
    yield from _schema_db(postgres_database_url, _INSTANCE_SQL)


@pytest.fixture
def typed_snap_db(postgres_database_url: str) -> Iterator[PostgresHubDatabase]:
    yield from _schema_db(postgres_database_url, _TYPED_SQL)


def _agent(
    name: str,
    steps: list[str] | list[WorkflowStep],
    variables: dict[str, Any] | None = None,
    *,
    exit_condition: str | None = None,
) -> AgentDefinitionBody:
    resolved = [
        step if isinstance(step, WorkflowStep) else WorkflowStep(name=step) for step in steps
    ]
    return AgentDefinitionBody(
        name=name,
        surfaces=["persona", "spawn"],
        step_workflow=AgentStepWorkflowBody(
            variables=dict(variables or {}),
            exit_condition=exit_condition if exit_condition is not None else f"{name}_done",
            steps=resolved,
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


def _snapshot_step_names(instance: Any) -> list[str]:
    return [step.name for step in instance.snapshot.steps]


def _enforcement_engine(db: PostgresHubDatabase) -> Any:
    from gobby.workflows.engine.enforcement_checks import EnforcementCheckMixin

    class _Engine(EnforcementCheckMixin):
        def __init__(self) -> None:
            self.db = db
            self.instance_manager = AgentStepInstanceManager(db)
            self._runner = None

        def _audit_step_tool_call(self, *args: object, **kwargs: object) -> None:
            return None

    return _Engine()


def test_definition_edit_does_not_mutate_running_snapshot(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance

    original = _agent("coder", ["claim", "implement"], {"goal": "v1"})
    persist_initial_step_instance(snap_db, original, session_id=S1, step_workflow_id=LINEAGE)
    edited = _agent("coder", ["claim", "implement", "review"], {"goal": "v2"})
    persist_initial_step_instance(snap_db, edited, session_id=S2, step_workflow_id=LINEAGE)

    running = AgentStepInstanceManager(snap_db).get_for_session(S1)
    nxt = AgentStepInstanceManager(snap_db).get_for_session(S2)
    assert running is not None
    assert nxt is not None
    assert _snapshot_step_names(running) == ["claim", "implement"]
    assert running.variables["goal"] == "v1"
    assert _snapshot_step_names(nxt) == ["claim", "implement", "review"]
    assert nxt.variables["goal"] == "v2"


def test_definition_delete_set_null_keeps_snapshot_enforcement(
    typed_snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance
    from gobby.storage.definitions.agents import AgentDefinitionManager
    from gobby.workflows.agent_resolver import resolve_agent_with_row
    from gobby.workflows.step_context import first_incomplete_step_workflow

    manager = AgentDefinitionManager(typed_snap_db)
    created = manager.upsert_with_steps(
        "coder",
        {"name": "coder", "surfaces": ["spawn"]},
        {
            "variables": {"goal": "ship"},
            "exit_condition": "done",
            "steps": [{"name": "claim", "allowed_tools": ["Read"]}],
        },
    )
    resolved = resolve_agent_with_row("coder", typed_snap_db)
    assert resolved is not None
    body, row = resolved
    persist_initial_step_instance(
        typed_snap_db,
        body,
        session_id=S1,
        step_workflow_id=row.step_workflow_id,
    )
    assert manager.hard_delete(created.id) is True

    remaining = AgentStepInstanceManager(typed_snap_db).get_for_session(S1)
    assert remaining is not None
    assert remaining.agent_step_workflow_id is None
    assert remaining.snapshot.exit_condition == "done"
    assert remaining.snapshot.steps[0].allowed_tools == ["Read"]

    incomplete = first_incomplete_step_workflow(typed_snap_db, S1)
    assert incomplete is not None
    assert incomplete.exit_condition == "done"

    event = MagicMock()
    event.data = {"tool_name": "Bash", "tool_input": {}}
    blocked = _enforcement_engine(typed_snap_db)._check_step_tool_enforcement(event, S1, {})
    assert blocked is not None
    assert blocked.decision == "block"
    assert "Read" in (blocked.reason or "")


def test_concurrent_spawns_keep_independent_snapshots(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance

    first = _agent("coder", ["claim", "implement"])
    persist_initial_step_instance(snap_db, first, session_id=S1, step_workflow_id=LINEAGE)
    persist_initial_step_instance(snap_db, first, session_id=S2, step_workflow_id=LINEAGE)
    renamed = _agent("coder", ["intake", "ship"])
    persist_initial_step_instance(
        snap_db,
        renamed,
        session_id="33333333-3333-4333-8333-333333333333",
        step_workflow_id=LINEAGE,
    )

    one = AgentStepInstanceManager(snap_db).get_for_session(S1)
    two = AgentStepInstanceManager(snap_db).get_for_session(S2)
    assert one is not None
    assert two is not None
    assert _snapshot_step_names(one) == ["claim", "implement"]
    assert _snapshot_step_names(two) == ["claim", "implement"]
    assert one.id != two.id


def test_project_scoped_override_is_snapshotted(typed_snap_db: PostgresHubDatabase) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body
    from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance
    from gobby.storage.definitions.agents import AgentDefinitionManager

    manager = AgentDefinitionManager(typed_snap_db)
    manager.upsert_with_steps(
        "coder",
        {"name": "coder", "surfaces": ["spawn"]},
        {
            "variables": {"scope": "global"},
            "exit_condition": "global_done",
            "steps": [{"name": "global_claim"}],
        },
    )
    project_row = manager.upsert_with_steps(
        "coder",
        {"name": "coder", "surfaces": ["spawn"], "goal": "project"},
        {
            "variables": {"scope": "project"},
            "exit_condition": "project_done",
            "steps": [{"name": "project_claim"}],
        },
        project_id=PROJECT,
    )
    loaded = _load_agent_body("coder", typed_snap_db, project_id=PROJECT)
    assert loaded is not None
    assert loaded.step_workflow is not None
    assert [step.name for step in loaded.step_workflow.steps] == ["project_claim"]
    persist_initial_step_instance(
        typed_snap_db,
        loaded,
        session_id=S1,
        step_workflow_id=project_row.step_workflow_id,
    )
    snap = AgentStepInstanceManager(typed_snap_db).get_for_session(S1)
    assert snap is not None
    assert _snapshot_step_names(snap) == ["project_claim"]
    assert snap.variables["scope"] == "project"
    assert snap.snapshot.exit_condition == "project_done"


@pytest.mark.asyncio
async def test_persona_same_agent_preserves_step_position(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

    manager = AgentStepInstanceManager(snap_db)
    instance = build_step_instance(
        _agent("alpha", ["claim", "implement"], {"goal": "ship"}),
        session_id=S1,
        step_workflow_id=LINEAGE,
        current_step="implement",
        variables={"goal": "ship", "progress": 2},
    )
    manager.save(instance)
    row = MagicMock()
    row.step_workflow_id = LINEAGE
    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(_agent("alpha", ["claim", "implement", "review"]), row),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
            return_value=({"_agent_type": "alpha"}, set()),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona._resolve_session_identity",
            return_value=(None, "claude"),
        ),
    ):
        result = await apply_persona_impl(agent="alpha", db=snap_db, session_id=S1)
    assert result["success"] is True
    kept = manager.get_for_session(S1)
    assert kept is not None
    assert kept.current_step == "implement"
    assert kept.variables["progress"] == 2
    assert _snapshot_step_names(kept) == ["claim", "implement"]


@pytest.mark.asyncio
async def test_persona_replace_fault_leaves_prior_instance(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
    from gobby.workflows.state_manager import SessionVariableManager

    manager = AgentStepInstanceManager(snap_db)
    manager.save(
        build_step_instance(_agent("alpha", ["claim"]), session_id=S1, step_workflow_id=LINEAGE)
    )
    SessionVariableManager(snap_db).merge_variables(S1, {"_agent_type": "alpha"})
    row = MagicMock()
    row.step_workflow_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(_agent("beta", ["review"]), row),
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
            AgentStepInstanceManager,
            "replace_for_session",
            side_effect=RuntimeError("replace failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="replace failed"):
            await apply_persona_impl(agent="beta", db=snap_db, session_id=S1)
    remaining = manager.get_for_session(S1)
    assert remaining is not None
    assert remaining.agent_name == "alpha"


@pytest.mark.asyncio
async def test_persona_switch_is_atomic_when_instance_write_fails_after_merge(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
    from gobby.workflows.state_manager import SessionVariableManager

    manager = AgentStepInstanceManager(snap_db)
    manager.save(
        build_step_instance(_agent("alpha", ["claim"]), session_id=S1, step_workflow_id=LINEAGE)
    )
    SessionVariableManager(snap_db).merge_variables(S1, {"_agent_type": "alpha"})
    row = MagicMock()
    row.step_workflow_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    original_merge = SessionVariableManager.merge_variables

    def _merge_then_fail(
        self: SessionVariableManager,
        session_id: str,
        updates: dict[str, Any],
    ) -> bool:
        original_merge(self, session_id, updates)
        raise RuntimeError("post-merge instance failed")

    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(_agent("beta", ["review"]), row),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
            return_value=({"_agent_type": "beta"}, set()),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona._resolve_session_identity",
            return_value=(None, "claude"),
        ),
        patch.object(SessionVariableManager, "merge_variables", _merge_then_fail),
    ):
        with pytest.raises(RuntimeError, match="post-merge instance failed"):
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
async def test_persona_stepless_switch_is_atomic_across_rows(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
    from gobby.workflows.state_manager import SessionVariableManager

    manager = AgentStepInstanceManager(snap_db)
    manager.save(
        build_step_instance(_agent("alpha", ["claim"]), session_id=S1, step_workflow_id=LINEAGE)
    )
    SessionVariableManager(snap_db).merge_variables(S1, {"_agent_type": "alpha"})
    row = MagicMock()
    row.step_workflow_id = None
    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(_stepless("comms"), row),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
            return_value=({"_agent_type": "comms"}, set()),
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
            await apply_persona_impl(agent="comms", db=snap_db, session_id=S1)
    remaining = manager.get_for_session(S1)
    assert remaining is not None
    assert remaining.agent_name == "alpha"


@pytest.mark.asyncio
async def test_persona_same_agent_missing_instance_persistence_fault_fails(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

    row = MagicMock()
    row.step_workflow_id = LINEAGE
    with (
        patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(_agent("alpha", ["claim"]), row),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
            return_value=({"_agent_type": "alpha"}, set()),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona._resolve_session_identity",
            return_value=(None, "claude"),
        ),
        patch.object(
            AgentStepInstanceManager,
            "replace_for_session",
            side_effect=RuntimeError("create failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="create failed"):
            await apply_persona_impl(agent="alpha", db=snap_db, session_id=S1)
    assert AgentStepInstanceManager(snap_db).get_for_session(S1) is None


@pytest.mark.asyncio
async def test_webchat_persona_failure_stops_runtime_and_skips_register() -> None:
    from tests.servers.websocket.chat.test_session import DummyMixin

    mixin = DummyMixin()
    mixin._pending_agents["conv-fail"] = "planner"
    mixin.web_chat_session_registry = MagicMock()
    mock_session = AsyncMock()
    mock_session.provider = "claude"
    mock_session.chat_mode = "plan"
    mock_session.db_session_id = None
    mock_session.resume_session_id = None
    mock_session.project_path = None
    mock_session.project_id = None
    mock_session.system_prompt_override = None
    mock_session.model = None
    mixin.web_chat_runtime_manager = MagicMock()
    mixin.web_chat_runtime_manager.create_session.return_value = mock_session
    mixin.web_chat_runtime_manager.policy_mismatch_reason.return_value = None
    mock_db_sess = MagicMock()
    mock_db_sess.id = "db-id-fail"
    mock_db_sess.seq_num = 7
    mock_db_sess.usage_output_tokens = 0
    mock_db_sess.chat_mode = "plan"
    mock_db_sess.approved_tools_json = None
    mock_db_sess.external_id = None
    mixin.session_manager = MagicMock()
    mixin.session_manager.db = MagicMock()
    mixin.session_manager.register.return_value = mock_db_sess
    mixin.session_manager.get.return_value = None
    agent_body = MagicMock()
    agent_body.name = "planner"
    agent_body.supports_surface.return_value = True
    with (
        patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
        patch(
            "gobby.workflows.agent_resolver.resolve_agent",
            return_value=agent_body,
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.build_session_persona_context",
            return_value=("## Role\nPlanner", None),
        ),
        patch(
            "gobby.mcp_proxy.tools.apply_persona.apply_persona_impl",
            new=AsyncMock(return_value={"success": False, "error": "snapshot failed"}),
        ),
    ):
        with pytest.raises(RuntimeError, match="snapshot failed"):
            await mixin._create_chat_session_inner("conv-fail")
    mock_session.stop.assert_awaited()
    mixin.web_chat_session_registry.register.assert_not_called()
    assert "conv-fail" not in mixin._chat_sessions


def test_compact_end_retains_instance_expired_end_deletes(
    snap_db: PostgresHubDatabase,
) -> None:
    from gobby.hooks.event_handlers._session_end import SessionEndMixin
    from gobby.hooks.events import HookEvent, HookEventType, SessionSource
    from gobby.hooks.hook_types import SessionEndReason
    from gobby.workflows.engine.core import RuleEngine

    manager = AgentStepInstanceManager(snap_db)
    manager.save(
        build_step_instance(
            _agent("alpha", ["claim", "implement"], {"goal": "ship"}),
            session_id=S1,
            step_workflow_id=LINEAGE,
            current_step="implement",
            variables={"goal": "ship", "progress": 2},
        )
    )

    class _Handler(SessionEndMixin):
        def __init__(self) -> None:
            self.logger = MagicMock()
            self._session_manager = None
            self._workflow_handler = cast(
                Any, SimpleNamespace(rule_engine=RuleEngine(db=snap_db))
            )
            self._session_storage = MagicMock()
            self._session_coordinator = None
            self._session_end_auto_link_worker = None
            self._session_message_processors: dict[str, Any] = {}
            self._task_manager = None
            self._worktree_manager = None
            self._skill_manager = None
            self._skills_config = None
            self._session_task_manager = None
            self._dispatch_session_summaries_fn = None
            self._call_tool = None
            self._get_machine_id = MagicMock(return_value=LOCAL_MACHINE_ID)
            self._resolve_project_id = MagicMock(return_value=None)
            self._handler_map = cast(Any, {})

    def _event(reason: SessionEndReason) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id=f"ext-{S1}",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(),
            data={"reason": reason.value},
            metadata={"_platform_session_id": S1},
        )

    handler = _Handler()
    with patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None):
        compact = handler.handle_session_end(_event(SessionEndReason.COMPACT))
    assert compact.decision == "allow"
    retained = manager.get_for_session(S1)
    assert retained is not None
    assert retained.session_id == S1
    assert retained.current_step == "implement"
    assert retained.variables == {"goal": "ship", "progress": 2}

    with patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None):
        expired = handler.handle_session_end(_event(SessionEndReason.CLEAR))
    assert expired.decision == "allow"
    assert manager.get_for_session(S1) is None


def test_daemon_stop_resume_keeps_step_and_variables(snap_db: PostgresHubDatabase) -> None:
    from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state

    manager = AgentStepInstanceManager(snap_db)
    manager.save(
        build_step_instance(
            _agent("alpha", ["claim", "implement"], {"goal": "ship"}),
            session_id=S1,
            step_workflow_id=LINEAGE,
            current_step="implement",
            variables={"goal": "ship", "progress": 3},
        )
    )
    retained = cleanup_agent_runtime_state(
        snap_db, run_id=None, child_session_id=S1, terminal_reason="daemon_stop"
    )
    assert retained.workflow_instance_rows == 0
    resumed = manager.get_for_session(S1)
    assert resumed is not None
    assert resumed.current_step == "implement"
    assert resumed.variables == {"goal": "ship", "progress": 3}

    deleted = cleanup_agent_runtime_state(
        snap_db, run_id=None, child_session_id=S1, terminal_reason="cancelled"
    )
    assert deleted.workflow_instance_rows == 1
    assert manager.get_for_session(S1) is None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tmux_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("TMUX", None)
    return env


def _tmux_cmd(*args: str) -> list[str]:
    return ["tmux", "-L", "gobby", "-f", "/dev/null", *args]


def _tmux_session_exists(name: str) -> bool:
    result = subprocess.run(
        _tmux_cmd("has-session", "-t", name),
        check=False,
        capture_output=True,
        env=_tmux_env(),
    )
    return result.returncode == 0


def _start_live_tmux_sleep() -> tuple[int, str]:
    name = f"gobby-snap-{uuid4().hex[:10]}"
    created = subprocess.run(
        _tmux_cmd("new-session", "-d", "-s", name, "--", "/bin/sleep", "60"),
        check=False,
        capture_output=True,
        text=True,
        env=_tmux_env(),
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr or created.stdout or "tmux new-session failed")
    pane = subprocess.check_output(
        _tmux_cmd("list-panes", "-t", name, "-F", "#{pane_pid}"),
        text=True,
        env=_tmux_env(),
    ).strip()
    return int(pane.splitlines()[0]), name


def _wait_until_dead(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} still alive after {timeout}s")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ("lease_attach", "live_pane", "start_run", "post_claim"),
)
async def test_post_launch_failure_terminates_process(
    fault: Literal["lease_attach", "live_pane", "start_run", "post_claim"],
    temp_db: Any,
    sample_git_project: dict[str, object],
) -> None:
    from gobby.agents.tmux import configure_tmux
    from gobby.config.tmux import TmuxConfig

    if shutil.which("tmux") is None:
        pytest.fail("tmux is required to prove post-launch process compensation")

    os.environ.pop("TMUX", None)
    configure_tmux(TmuxConfig())
    sample_project = sample_git_project
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        outcome = await _run_post_launch_failure_case(
            fault,
            temp_db,
            sample_project,
        )
    assert outcome["success"] is False
    assert outcome["pid_alive"] is False
    assert outcome["tmux_exists"] is False
    assert outcome["mutex"] is None
    assert outcome["mutex_by_run"] is None


async def _run_post_launch_failure_case(
    fault: Literal["lease_attach", "live_pane", "start_run", "post_claim"],
    temp_db: Any,
    sample_project: dict[str, object],
) -> dict[str, Any]:
    from gobby.agents.isolation import IsolationContext
    from gobby.agents.session import ChildSessionManager
    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=str(sample_project["id"]),
        title="Post-launch fault task",
        validation_criteria="Observable spawn rollback after a live process starts.",
    )
    session_manager = SessionManager(temp_db)
    parent_session_id = session_manager.register_session(
        external_id=f"parent-{fault}",
        machine_id=LOCAL_MACHINE_ID,
        source="test",
        project_id=str(sample_project["id"]),
        title="Parent",
    )
    child_manager = ChildSessionManager(session_manager)
    run_storage = LocalAgentRunManager(temp_db)
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.child_session_manager = child_manager
    runner._child_session_manager = child_manager
    runner.run_storage = run_storage
    runner.agent_lifecycle_monitor = None
    runner.task_manager = task_manager

    def cancel_run(run_id: str) -> bool:
        return run_storage.cancel(run_id) is not None

    runner.cancel_run.side_effect = cancel_run
    live: dict[str, Any] = {}

    async def execute_spawn(request: Any) -> SimpleNamespace:
        pid, tmux_name = _start_live_tmux_sleep()
        live["pid"] = pid
        live["tmux"] = tmux_name
        live["child_session_id"] = request.prepared_spawn.session_id
        live["run_id"] = request.prepared_spawn.agent_run_id
        assert _pid_alive(pid)
        assert _tmux_session_exists(tmux_name)
        return SimpleNamespace(
            success=True,
            child_session_id=request.prepared_spawn.session_id,
            status="pending",
            pid=pid,
            terminal_type="tmux",
            tmux_session_name=tmux_name,
            tmux_socket_name="gobby",
            tmux_socket_path=None,
            message="dummy live spawn",
            error=None,
        )

    extra_patches: list[Any] = []
    if fault == "lease_attach":
        extra_patches.append(
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.TaskSpawnLease.attach",
                return_value="dispatch mutex row disappeared",
            )
        )
    if fault == "start_run":
        extra_patches.append(
            patch.object(LocalAgentRunManager, "start", side_effect=RuntimeError("start failed"))
        )
    if fault == "post_claim":
        extra_patches.append(
            patch.object(LocalTaskManager, "claim_task", side_effect=RuntimeError("claim failed"))
        )
    pane_return = (False, "fatal pane output") if fault == "live_pane" else (True, None)
    mock_handler = MagicMock()
    mock_handler.prepare_environment = AsyncMock(
        return_value=IsolationContext(cwd=str(sample_project["repo_path"]))
    )
    mock_handler.cleanup_environment = AsyncMock()
    mock_handler.build_context_prompt.return_value = "Test prompt"

    try:
        with ExitStack() as stack:
            mock_ctx = stack.enter_context(
                patch("gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context")
            )
            stack.enter_context(
                patch(
                    "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler",
                    return_value=mock_handler,
                )
            )
            stack.enter_context(
                patch(
                    "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                    new=AsyncMock(side_effect=execute_spawn),
                )
            )
            stack.enter_context(
                patch(
                    "gobby.mcp_proxy.tools.spawn_agent._implementation._check_tmux_session_alive",
                    new_callable=AsyncMock,
                    return_value=pane_return,
                )
            )
            stack.enter_context(
                patch(
                    "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
                    return_value=LOCAL_MACHINE_ID,
                )
            )
            stack.enter_context(
                patch("gobby.mcp_proxy.tools.spawn_agent._health.schedule_tmux_health_check")
            )
            for extra in extra_patches:
                stack.enter_context(extra)
            mock_ctx.return_value = {
                "id": str(sample_project["id"]),
                "project_path": str(sample_project["repo_path"]),
            }
            result = await spawn_agent_impl(
                prompt="Test prompt",
                runner=runner,
                agent_body=AgentDefinitionBody(name="default", provider="claude"),
                task_id=task.id,
                task_manager=task_manager,
                isolation="none",
                parent_session_id=parent_session_id,
                project_path=str(sample_project["repo_path"]),
                session_manager=session_manager,
                db=temp_db,
            )
        _wait_until_dead(int(live["pid"]))
        mutex = TaskDispatchMutexManager(temp_db)
        return {
            "success": result["success"],
            "pid_alive": _pid_alive(int(live["pid"])),
            "tmux_exists": _tmux_session_exists(str(live["tmux"])),
            "mutex": mutex.get_mutex(task.id),
            "mutex_by_run": mutex.get_mutex_by_run_id(str(live["run_id"])),
        }
    finally:
        tmux_name = live.get("tmux")
        if isinstance(tmux_name, str) and _tmux_session_exists(tmux_name):
            subprocess.run(
                _tmux_cmd("kill-session", "-t", tmux_name),
                check=False,
                capture_output=True,
                env=_tmux_env(),
            )
        pid = live.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
