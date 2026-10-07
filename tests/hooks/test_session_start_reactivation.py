"""Compaction and relaunch preserve the active seat's complete definition."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session_start.agents import (
    activate_default_agent,
    build_agent_changes,
    resolve_agent_name,
)
from gobby.hooks.event_handlers._session_start.flow import handle_pre_created_session
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.mcp_proxy.tools.apply_agent_definition import apply_agent_definition_impl
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager
from gobby.workflows.definitions import AgentDefinitionBody, AgentSelector, AgentStepWorkflowBody
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows
from tests.mcp_proxy.tools.test_apply_agent_definition import (
    inject_definition_context,
    register_session,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def session_id(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> str:
    return register_session(temp_db, monkeypatch)


@pytest.fixture
def seats(temp_db: HubDatabase) -> dict[str, AgentDefinitionBody]:
    LocalSkillManager(temp_db).create_skill(
        name="seat-skill", description="Seat", content="# Seat", enabled=True
    )
    RuleDefinitionManager(temp_db).create(
        name="seat-rule", definition_json={"event": "before_tool", "effects": []}
    )
    bodies = {
        name: make_agent_definition(
            name=name,
            provider="inherit",
            surfaces=["persona"],
            prompts={"persona": name},
            workflows=make_agent_workflows(
                variables={"x_only": 1} if name == "x" else {},
                rule_selectors=AgentSelector(include=["name:seat-rule"] if name == "x" else []),
                skill_selectors=(
                    AgentSelector(include=["name:seat-skill"])
                    if name == "x"
                    else AgentSelector(include=[])
                    if name == "default"
                    else None
                ),
                skill_format="summary" if name == "default" else None,
            ),
        )
        for name in ("default", "x", "y")
    }
    for name, body in bodies.items():
        AgentDefinitionManager(temp_db).create(
            name=name, definition_json=body.model_dump_json(), source="custom"
        )
    return bodies


def activation_handler(db: HubDatabase) -> Any:
    handler = SimpleNamespace(
        _session_manager=SessionManager(db),
        _session_coordinator=None,
        logger=logging.getLogger(__name__),
        _derive_transcript_path=lambda *args, **kwargs: None,
        _setup_code_index=MagicMock(),
        _resolve_message_processor=lambda: None,
        _build_claimed_task_context=lambda *args, **kwargs: None,
        _compose_session_response=lambda **kwargs: HookResponse(decision="allow"),
    )
    handler._resolve_agent_name = lambda sid, override, existing=None: resolve_agent_name(
        handler, sid, override, existing
    )
    handler._build_agent_changes = lambda *args: build_agent_changes(handler, *args)
    handler._activate_default_agent = lambda *args, **kwargs: activate_default_agent(
        handler, *args, **kwargs
    )
    return handler


def compact(db: HubDatabase, sid: str, source: str = "compact") -> None:
    handler = activation_handler(db)
    session = SessionManager(db).get(sid)
    assert session is not None
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=sid,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"source": source},
        machine_id=session.machine_id,
    )
    with ExitStack() as stack:
        for name in (
            "discover_and_bind_external_terminal",
            "expire_stale_terminal_sessions_for_context",
            "retry_native_terminal_bind",
            "rehydrate_found_work_gate_arm",
            "seed_user_profile_content",
            "_seed_parent_turn_seq",
            "_consume_pending_handoff_compact_continuation",
            "_log_session_start_lifecycle",
            "clear_queued_context",
            "bump_stop_replay_epoch",
        ):
            stack.enter_context(patch(f"gobby.hooks.event_handlers._session_start.flow.{name}"))
        stack.enter_context(
            patch(
                "gobby.hooks.event_handlers._session_start.flow.classify_session_start_context",
                return_value=SimpleNamespace(mode="live"),
            )
        )
        handle_pre_created_session(
            handler, session, session.external_id, None, "codex", event, None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["compact", "resume"])
async def test_reactivation_reports_definition_drift_once(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody], source: str
) -> None:
    from gobby.mcp_proxy.tools.apply_agent_definition import definition_pin
    from gobby.workflows.agent_resolver import resolve_agent

    assert (await apply_agent_definition_impl("x", temp_db, session_id))["success"]
    assert inject_definition_context(temp_db, session_id) == "x"
    manager = SessionVariableManager(temp_db)
    old_pin = manager.get_variables(session_id)["_agent_definition_hash"]
    manager.merge_variables(session_id, {"_agent_definition_drift": None})
    seats["x"].prompts.persona = "Changed seat prompt."
    row = AgentDefinitionManager(temp_db).get_by_name("x")
    assert row is not None
    AgentDefinitionManager(temp_db).update(row.id, definition_json=seats["x"].model_dump_json())
    compact(temp_db, session_id, source)
    after = manager.get_variables(session_id)
    resolved = resolve_agent("x", temp_db, cli_source="codex")
    assert resolved is not None
    new_pin = definition_pin(resolved)
    assert new_pin != old_pin
    line = (
        f"Definition `x` changed since this session activated it (`{old_pin[:12]}` → "
        f"`{new_pin[:12]}`); the current definition now applies."
    )
    assert after["_agent_definition_hash"] == new_pin
    assert after.get("_agent_definition_drift") == line
    assert after["_agent_identity_reinject"] is True
    compact(temp_db, session_id, source)
    assert manager.get_variables(session_id)["_agent_definition_drift"] == line
    context = inject_definition_context(temp_db, session_id)
    assert context.startswith("Changed seat prompt.")
    assert context.count(line) == 1
    assert manager.get_variables(session_id)["_agent_definition_drift"] is None
    assert inject_definition_context(temp_db, session_id) == ""
    compact(temp_db, session_id, source)
    assert "changed since" not in inject_definition_context(temp_db, session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["compact", "resume"])
async def test_unchanged_pin_injects_no_drift_line(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody], source: str
) -> None:
    result = await apply_agent_definition_impl("x", temp_db, session_id)
    assert result["success"]
    assert seats["x"].provider == "inherit"
    assert inject_definition_context(temp_db, session_id) == "x"
    compact(temp_db, session_id, source)
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["_agent_definition_hash"] == result["definition_hash"]
    assert not variables.get("_agent_definition_drift")
    assert "changed since" not in inject_definition_context(temp_db, session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["compact", "resume"])
async def test_drift_reactivation_keeps_running_step_instance(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody], source: str
) -> None:
    from gobby.mcp_proxy.tools.apply_agent_definition import definition_pin
    from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance
    from gobby.workflows.agent_resolver import resolve_agent
    from gobby.workflows.step_instances import AgentStepInstanceManager

    body = seats["x"]
    body.step_workflow = AgentStepWorkflowBody(steps=[{"name": "claim"}, {"name": "implement"}])
    definitions = AgentDefinitionManager(temp_db)
    row = definitions.get_by_name("x")
    assert row is not None
    definitions.update(row.id, definition_json=body.model_dump_json())
    persist_initial_step_instance(temp_db, body, session_id=session_id, step_workflow_id=None)
    assert (await apply_agent_definition_impl("x", temp_db, session_id))["success"]
    assert inject_definition_context(temp_db, session_id) == "x"
    instances = AgentStepInstanceManager(temp_db)
    before = instances.get_for_session(session_id)
    assert before is not None
    before.current_step = "implement"
    instances.save(before)
    body.workflows.rule_selectors = AgentSelector(include=[])
    body.blocked_tools = ["Write"]
    body.blocked_mcp_tools = ["gobby-worktrees:create_worktree"]
    body.step_workflow = AgentStepWorkflowBody(steps=[{"name": "replacement"}])
    definitions.update(row.id, definition_json=body.model_dump_json())
    compact(temp_db, session_id, source)
    after = SessionVariableManager(temp_db).get_variables(session_id)
    resolved = resolve_agent("x", temp_db, cli_source="codex")
    assert resolved is not None
    assert after["_agent_definition_hash"] == definition_pin(resolved)
    assert after["_active_rule_names"] == []
    assert after["_agent_blocked_tools"] == ["Write"]
    assert after["_agent_blocked_mcp_tools"] == ["gobby-worktrees:create_worktree"]
    running = instances.get_for_session(session_id)
    assert running is not None
    assert running.id == before.id
    assert running.current_step == "implement"
    assert running.snapshot == before.snapshot
    line = after.get("_agent_definition_drift")
    assert isinstance(line, str) and "changed since" in line
    assert inject_definition_context(temp_db, session_id).count(line) == 1
    assert inject_definition_context(temp_db, session_id) == ""


@pytest.mark.asyncio
async def test_compact_sessionstart_keeps_seat_skills_and_rules(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody]
) -> None:
    result = await apply_agent_definition_impl("x", temp_db, session_id)
    assert result["success"]
    manager = SessionVariableManager(temp_db)
    before = manager.get_variables(session_id)
    assert before["_active_skill_names"] == ["seat-skill"]
    assert before["_active_rule_names"] == ["seat-rule"]
    compact(temp_db, session_id)
    after = manager.get_variables(session_id)
    for key in ("_agent_type", "_active_skill_names", "_active_rule_names"):
        assert after[key] == before[key]
    assert after["_agent_type"] == "x"


@pytest.mark.parametrize("base", ["default", "x"])
@pytest.mark.parametrize("source", ["compact", "resume"])
@pytest.mark.parametrize("persona", ["y", "overlay"])
async def test_persona_overlay_survives_sessionstart_and_default_restores_seat(
    temp_db: HubDatabase,
    session_id: str,
    seats: dict[str, AgentDefinitionBody],
    base: str,
    source: str,
    persona: str,
) -> None:
    from gobby.hooks.event_handlers._agent import AgentEventHandlerMixin
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

    LocalSkillManager(temp_db).create_skill(
        name="persona-skill", description="Persona", content="Persona", enabled=True
    )
    overlay = make_agent_definition(
        name="overlay",
        surfaces=["persona"],
        prompts={"persona": "overlay"},
        workflows=make_agent_workflows(
            skill_selectors=AgentSelector(include=["name:persona-skill"]), skill_format="compact"
        ),
    )
    AgentDefinitionManager(temp_db).create(
        name="overlay", definition_json=overlay.model_dump_json()
    )
    result = await apply_agent_definition_impl(base, temp_db, session_id)
    assert result["success"]
    manager = SessionVariableManager(temp_db)
    before = manager.get_variables(session_id)
    assert (await apply_persona_impl(persona, temp_db, session_id))["success"]
    compact(temp_db, session_id, source)
    after = manager.get_variables(session_id)
    assert after["_persona_name"] == persona
    assert after["_active_skill_names"] == (["persona-skill"] if persona == "overlay" else None)
    assert after["_skill_format"] == ("compact" if persona == "overlay" else None)

    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=session_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
    )
    response = HookResponse(decision="allow")
    AgentEventHandlerMixin._inject_agent_instructions_if_needed(
        activation_handler(temp_db), event, session_id, response
    )
    assert response.context == persona
    for key in ("_agent_type", "_active_rule_names", "_agent_blocked_tools", "x_only"):
        assert after.get(key) == before.get(key)
    assert (await apply_persona_impl("default", temp_db, session_id))["success"]
    restored = manager.get_variables(session_id)
    assert restored["_persona_name"] is None
    assert restored["_active_skill_names"] == before["_active_skill_names"]
    assert restored["_skill_format"] == before["_skill_format"]
    assert restored["_agent_type"] == base
    response = HookResponse(decision="allow")
    AgentEventHandlerMixin._inject_agent_instructions_if_needed(
        activation_handler(temp_db), event, session_id, response
    )
    assert response.context == base


async def test_writing_definition_activation_clears_persona_overlay(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody]
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

    assert (await apply_persona_impl("y", temp_db, session_id))["success"]
    assert (await apply_agent_definition_impl("x", temp_db, session_id))["success"]
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["_persona_name"] is None
    assert variables["_active_skill_names"] == ["seat-skill"]


@pytest.mark.asyncio
async def test_compact_preserves_defaults_and_reapplies_definition_variables(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody]
) -> None:
    from gobby.storage.definitions.variables import SessionVariableDefaultManager

    SessionVariableDefaultManager(temp_db).create(name="task_claimed", default_value=False)
    await apply_agent_definition_impl("x", temp_db, session_id)
    manager = SessionVariableManager(temp_db)
    manager.merge_variables(session_id, {"task_claimed": True, "x_only": 99})
    compact(temp_db, session_id)
    stored = manager.get_variables(session_id)
    assert stored["task_claimed"] is True
    assert stored["x_only"] == 1


@pytest.mark.asyncio
async def test_inherit_provider_pin_is_stable_across_entry_points(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody]
) -> None:
    result = await apply_agent_definition_impl("x", temp_db, session_id)
    compact(temp_db, session_id)
    assert (
        SessionVariableManager(temp_db).get_variables(session_id)["_agent_definition_hash"]
        == result["definition_hash"]
    )
    with patch.object(SessionVariableManager, "merge_variables") as merge:
        repeated = await apply_agent_definition_impl("x", temp_db, session_id)
    assert repeated["status"] == "unchanged"
    merge.assert_not_called()


@pytest.mark.asyncio
async def test_reactivation_clears_dropped_skill_restriction(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody]
) -> None:
    manager = SessionVariableManager(temp_db)
    seats["x"].workflows.skill_selectors = AgentSelector(include=[])
    seats["x"].workflows.skill_format = "summary"
    definitions = AgentDefinitionManager(temp_db)
    row = definitions.get_by_name("x")
    assert row is not None
    definitions.update(row.id, definition_json=seats["x"].model_dump_json())
    await apply_agent_definition_impl("x", temp_db, session_id)
    assert manager.get_variables(session_id)["_active_skill_names"] == []
    assert manager.get_variables(session_id)["_skill_format"] == "summary"
    seats["x"].workflows.skill_selectors = None
    seats["x"].workflows.skill_format = None
    definitions.update(row.id, definition_json=seats["x"].model_dump_json())
    compact(temp_db, session_id)
    after = manager.get_variables(session_id)
    assert after["_active_skill_names"] is None
    assert after["_skill_format"] is None


@pytest.mark.asyncio
async def test_drift_then_relaunch_clears_retired_definition_key(
    temp_db: HubDatabase, session_id: str, seats: dict[str, AgentDefinitionBody]
) -> None:
    await apply_agent_definition_impl("x", temp_db, session_id)
    seats["x"].workflows.variables = {}
    row = AgentDefinitionManager(temp_db).get_by_name("x")
    assert row is not None
    AgentDefinitionManager(temp_db).update(row.id, definition_json=seats["x"].model_dump_json())
    compact(temp_db, session_id)
    variables = SessionVariableManager(temp_db)
    assert variables.get_variables(session_id)["x_only"] == 1
    assert "x_only" in variables.get_variables(session_id)["_agent_definition_keys"]
    result = await apply_agent_definition_impl("y", temp_db, session_id, relaunch=True)
    assert result["status"] == "applied"
    assert variables.get_variables(session_id)["x_only"] is None


@pytest.mark.parametrize("target", ["y", "default"])
def test_stale_sessionstart_behind_relaunch_keeps_current_seat(
    temp_db: HubDatabase,
    session_id: str,
    seats: dict[str, AgentDefinitionBody],
    target: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    asyncio.run(apply_agent_definition_impl("x", temp_db, session_id))
    handler = activation_handler(temp_db)
    prepared = threading.Event()
    released = threading.Event()
    build = handler._build_agent_changes
    results: list[Any] = []
    errors: list[BaseException] = []

    def delayed(*args: Any) -> Any:
        delta = build(*args)
        prepared.set()
        assert released.wait(5)
        return delta

    def reactivate() -> None:
        try:
            results.append(activate_default_agent(handler, session_id, "codex", None))
        except BaseException as error:
            errors.append(error)

    handler._build_agent_changes = delayed
    thread = threading.Thread(target=reactivate)
    thread.start()
    try:
        assert prepared.wait(5)
        result = asyncio.run(
            apply_agent_definition_impl(target, temp_db, session_id, relaunch=True)
        )
        assert result["status"] == "applied"
        relaunched = SessionVariableManager(temp_db).get_variables(session_id)
    finally:
        released.set()
        thread.join(10)
    assert not thread.is_alive()
    assert errors == []
    assert results == [None]
    assert SessionVariableManager(temp_db).get_variables(session_id) == relaunched
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert session_id in message
    assert f"keeps agent {target}" in message
    assert "stale activation of x" in message
