"""Semantic identity and provider-native scratch behavior at clear boundaries."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.agent_workspaces import grant_seat_workspace_paths, spawn_workspace_write_paths
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.workspace_bindings import bind_session_workspace
from gobby.app_context import ServiceContainer
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._session_start.agents import resolve_agent_name
from gobby.hooks.event_handlers._session_start.flow import handle_pre_created_session
from gobby.hooks.event_handlers.clear_identity import reset_operator_identity, stop_spawned_clear
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.apply_agent_definition import apply_agent_definition_impl
from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
from gobby.sessions.clear_continuation import take_clear_handoff_marker
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import TASK_EDIT_LEDGER_KEYS
from tests.hooks.test_clear_successor_seat import (
    _define,
    _pane,
    _seat_handler,
    _spawned_seat,
    _stage,
    _start,
)
from tests.hooks.test_session_materialize import _ATTEMPT_ID

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_clear_reapplies_after_a_persona_overlay(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "persona-clear")
    _define(temp_db, ["claim"])
    reset_operator_identity(temp_db, pane.predecessor_id)
    assert (await apply_persona_impl("seat", temp_db, pane.predecessor_id))["success"]
    variables = SessionVariableManager(temp_db)
    assert variables.get_variables(pane.predecessor_id)["_persona_name"] == "seat"
    reset_operator_identity(temp_db, pane.predecessor_id)
    after = variables.get_variables(pane.predecessor_id)
    assert after["_agent_type"] == "default"
    assert after.get("_persona_name") is None


@pytest.mark.asyncio
async def test_default_activation_keeps_clear_recovery_on_default(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "default-recovery")
    AgentDefinitionManager(temp_db).create(
        name="default",
        definition_json={
            "name": "default",
            "surfaces": ["persona"],
            "prompts": {"persona": "Default instructions."},
            "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        },
    )
    reset_operator_identity(temp_db, pane.predecessor_id)
    assert (await apply_agent_definition_impl("default", temp_db, pane.predecessor_id))["success"]
    with patch(
        "gobby.storage.config_repository.ConfigRepository.read",
        return_value=SimpleNamespace(values={"default_agent": "fixture-configured-seat"}),
    ):
        assert (
            resolve_agent_name(_seat_handler(pane.sessions), pane.predecessor_id, None) == "default"
        )


@pytest.mark.parametrize("change", ["updated", "refreshed", "deleted"])
@pytest.mark.asyncio
async def test_clear_resets_activated_variables_after_definition_drift(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "definition-drift")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    _define(temp_db, ["claim"])
    SessionVariableDefaultManager(temp_db).create(name="seat_only", default_value=False)
    assert (await apply_agent_definition_impl("seat", temp_db, pane.predecessor_id))["success"]
    definitions = AgentDefinitionManager(temp_db)
    definition = definitions.get_by_name("seat")
    assert definition is not None
    if change == "deleted":
        definitions.delete(definition.id)
    else:
        assert isinstance(definition.definition_json, dict)
        body = dict(definition.definition_json)
        body["workflows"] = {**body.get("workflows", {}), "variables": {}}
        definitions.update(definition.id, definition_json=body)
        if change == "refreshed":
            assert (await apply_agent_definition_impl("seat", temp_db, pane.predecessor_id))[
                "success"
            ]
    reset_operator_identity(temp_db, pane.predecessor_id)
    after = SessionVariableManager(temp_db).get_variables(pane.predecessor_id)
    assert after["_agent_type"] == "default"
    assert after["seat_only"] is False


@pytest.mark.asyncio
async def test_clear_preserves_runtime_ledgers_and_safeguard_defaults(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "runtime-clear")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    _define(temp_db, ["claim"])
    SessionVariableDefaultManager(temp_db).create(name="require_uv", default_value=True)
    SessionVariableDefaultManager(temp_db).create(name="seat_only", default_value=False)
    assert (await apply_agent_definition_impl("seat", temp_db, pane.predecessor_id))["success"]
    variables = SessionVariableManager(temp_db)
    runtime: dict[str, Any] = {
        key: {"task-id": {"owned.py": "evidence"}} for key in TASK_EDIT_LEDGER_KEYS
    }
    runtime["task_edited_files"] = {"task-id": ["owned.py"]}
    runtime.update({"require_uv": True, "session_unclaimed_edits": ["dirty.py"]})
    variables.merge_variables(
        pane.predecessor_id,
        {
            **runtime,
            "_agent_type": "seat",
            "seat_only": True,
            "_agent_definition_keys": [*runtime, "seat_only"],
        },
    )
    reset_operator_identity(temp_db, pane.predecessor_id)
    after = variables.get_variables(pane.predecessor_id)
    assert {key: after.get(key) for key in runtime} == runtime
    assert after.get("seat_only") is False
    assert after["_agent_type"] == "default"


@pytest.mark.asyncio
async def test_spawned_clear_uses_shared_cancellation_service(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "cancel-service")
    run, _ = _spawned_seat(temp_db, pane)
    called = asyncio.Event()

    async def cancelled(**kwargs: Any) -> dict[str, bool]:
        called.set()
        return {"success": True}

    services = ServiceContainer(
        database=temp_db,
        session_manager=pane.sessions,
        task_manager=MagicMock(),
        agent_runner=MagicMock(),
        main_loop=asyncio.get_running_loop(),
    )
    with (
        patch("gobby.app_context.get_app_context", return_value=services),
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.stop_agent_run",
            new_callable=AsyncMock,
            side_effect=cancelled,
        ) as stop,
    ):
        assert stop_spawned_clear(pane.sessions.get(pane.predecessor_id))
        await asyncio.wait_for(called.wait(), 1)
    stop.assert_awaited_once()
    assert stop.await_args is not None
    assert stop.await_args.kwargs["run_id"] == run.id
    assert stop.await_args.kwargs["session_manager"] is pane.sessions


def test_precreated_spawned_clear_never_activates_a_successor(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "precreated-clear")
    _spawned_seat(temp_db, pane)
    handler = MagicMock()
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="pred-ext",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data={"source": "clear"},
        machine_id=pane.machine_id,
    )
    with patch("gobby.hooks.event_handlers.clear_identity.stop_spawned_clear", return_value=True):
        response = handle_pre_created_session(
            handler,
            pane.sessions.get(pane.predecessor_id),
            "pred-ext",
            None,
            "grok",
            event,
            pane.root,
        )
    assert response.decision == "block"
    handler._session_manager.update.assert_not_called()
    handler._activate_default_agent.assert_not_called()


@pytest.mark.parametrize("reason,expected", [("clear", "default"), ("compact", "seat")])
@pytest.mark.asyncio
async def test_operator_end_resets_only_on_clear(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_dependencies: dict[str, Any],
    reason: str,
    expected: str,
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "operator-end")
    _define(temp_db, ["claim"])
    assert (await apply_agent_definition_impl("seat", temp_db, pane.predecessor_id))["success"]
    handlers = EventHandlers(
        **{**mock_dependencies, "session_storage": pane.sessions, "session_manager": pane.sessions}
    )
    event = HookEvent(
        event_type=HookEventType.SESSION_END,
        session_id="pred-ext",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data={"reason": reason},
        machine_id=pane.machine_id,
        metadata={"_platform_session_id": pane.predecessor_id},
    )
    handlers.handle_session_end(event)
    handlers.handle_session_end(event)
    after = SessionVariableManager(temp_db).get_variables(pane.predecessor_id)
    assert after["_agent_type"] == expected


@pytest.mark.asyncio
async def test_spawned_clear_dispatches_cancellation_without_completing_task(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_dependencies: dict[str, Any],
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "spawned-end")
    _spawned_seat(temp_db, pane)
    _stage(temp_db, pane)
    handlers = EventHandlers(
        **{**mock_dependencies, "session_storage": pane.sessions, "session_manager": pane.sessions}
    )
    with patch(
        "gobby.hooks.event_handlers.clear_identity.stop_spawned_clear", return_value=True
    ) as stop:
        handlers.handle_session_end(
            HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id="pred-ext",
                source=SessionSource.GROK,
                timestamp=datetime.now(UTC),
                data={"reason": "clear"},
                machine_id=pane.machine_id,
                metadata={"_platform_session_id": pane.predecessor_id},
            )
        )
    stop.assert_called_once()
    ended = pane.sessions.get(pane.predecessor_id)
    assert ended is not None and ended.status == "expired"
    mock_dependencies["session_coordinator"].complete_agent_run.assert_not_called()
    mock_dependencies["task_manager"].close_task.assert_not_called()


def test_spawned_clear_cannot_transfer_run(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "spawned-clear")
    run, _ = _spawned_seat(temp_db, pane)
    _stage(temp_db, pane)
    successor = pane.register("forbidden-successor")
    assert not take_clear_handoff_marker(
        temp_db, pane.predecessor_id, attempt_id=_ATTEMPT_ID, successor_id=successor
    )
    predecessor_row = pane.sessions.get(pane.predecessor_id)
    successor_row = pane.sessions.get(successor)
    assert predecessor_row is not None and predecessor_row.agent_run_id == run.id
    assert successor_row is not None and successor_row.agent_run_id is None


def test_default_launch_grants_no_agent_workspace(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "default-launch")
    SessionVariableManager(temp_db).merge_variables(pane.predecessor_id, {"_agent_type": "default"})
    assert spawn_workspace_write_paths(temp_db, pane.predecessor_id, "default") == []
    config = grant_seat_workspace_paths(SandboxConfig(), temp_db, pane.predecessor_id, "default")
    assert not config.extra_write_paths
    assert not (tmp_path / "home" / "agents_workspaces").exists()


@pytest.mark.asyncio
async def test_operator_clear_handoff_resets_definition(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "operator-clear")
    _define(temp_db, ["claim"])
    assert (await apply_agent_definition_impl("seat", temp_db, pane.predecessor_id))["success"]
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        pane.predecessor_id,
        {"_persona_name": "fixture-overlay", "_agent_workspace_injected": True},
    )
    _stage(temp_db, pane)
    successor = _start(pane, "cleared-thread")
    after = variables.get_variables(successor)
    assert after["_agent_type"] == "default"
    assert not after.get("seat_only")
    assert not after.get("_persona_name")
    assert not after.get("_agent_workspace_dir")
    assert not after.get("_agent_workspace_injected")
    assert after["is_spawned_agent"] is False


def test_default_session_has_no_agent_workspace(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    pane = _pane(temp_db, tmp_path, monkeypatch, "default-scratch")
    manager = SessionVariableManager(temp_db)
    manager.merge_variables(pane.predecessor_id, {"_agent_type": "default"})
    assert bind_session_workspace(temp_db, pane.predecessor_id) is None
    assert not manager.get_variables(pane.predecessor_id).get("_agent_workspace_dir")
    assert not (home / "agents_workspaces").exists()
