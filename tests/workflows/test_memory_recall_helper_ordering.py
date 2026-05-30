"""Cross-rule integration tests for the memory recall helper turn pipeline."""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.effects import EffectsMixin
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

RULES_ROOT = Path("src/gobby/install/shared/workflows/rules")
MEMORY_RULES_ROOT = RULES_ROOT / "memory-lifecycle"
MESSAGING_RULES_ROOT = RULES_ROOT / "messaging"

CANCEL_RULE_NAME = "cancel-stale-memory-recall-helpers"
DELIVER_RULE_NAME = "deliver-pending-messages"
SPAWN_RULE_NAME = "spawn-memory-recall-helper"
INCREMENT_RULE_NAME = "increment-parent-turn-seq"

PLATFORM_SESSION_ID = "platform-Y"
EXTERNAL_SESSION_ID = "external-X"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


class _IdentitySessionManager:
    def resolve_session_reference(self, ref: str, project_id: str | None = None) -> str:
        del project_id
        return ref

    def get(self, session_id: str) -> Any:
        return MagicMock(id=session_id, agent_depth=0, external_id=None)


class _DbBackedRunner:
    def __init__(self, run_manager: LocalAgentRunManager) -> None:
        self.run_storage = run_manager

    def get_run(self, run_id: str) -> Any:
        return self.run_storage.get(run_id)

    def cancel_run(self, run_id: str) -> bool:
        return self.run_storage.cancel(run_id, terminal_reason="user_cancelled") is not None


def _sync_rules(db: HubDatabase, tmp_path: Path, rule_names: list[str]) -> None:
    rules_root = tmp_path / f"rules-{time.monotonic_ns()}"
    for rule_name in rule_names:
        source_dir = MESSAGING_RULES_ROOT if rule_name == DELIVER_RULE_NAME else MEMORY_RULES_ROOT
        target_dir = rules_root / source_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        rule_source = source_dir / f"{rule_name}.yaml"
        shutil.copy2(rule_source, target_dir / rule_source.name)

    result = sync_bundled_rules(db, rules_path=rules_root)
    assert result["errors"] == []


def _platform_event(
    prompt: str = "six or more words for helper spawn rule",
    **data_overrides: Any,
) -> HookEvent:
    data: dict[str, Any] = {"prompt": prompt}
    data.update(data_overrides)
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": PLATFORM_SESSION_ID},
    )


def _base_variables(**overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "memory_recall_helper_enabled": True,
        "parent_turn_seq": 7,
        "servers_listed": True,
    }
    variables.update(overrides)
    return variables


def _cancel_payload() -> dict[str, Any]:
    return {"success": True, "cancelled": [], "errors": [], "count": 0}


def _deliver_payload(messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    messages = [] if messages is None else messages
    return {"success": True, "messages": messages, "count": len(messages)}


def _envelope(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    del tool
    return {"success": True, "inject_result": True, "result": payload}


def _memory_recall_message(
    memory_id: str,
    content: str,
    *,
    origin_turn_seq: int,
    from_session: str = "child-A",
    rationale: str = "selected",
) -> dict[str, Any]:
    return {
        "from_session": from_session,
        "content": json.dumps(
            {
                "type": "memory_recall",
                "origin_turn_seq": origin_turn_seq,
                "memories": [{"id": memory_id, "content": content}],
                "rationale": rationale,
            }
        ),
    }


def _context(response: Any) -> str:
    return response.context or ""


def _mcp_calls(response: Any) -> list[dict[str, Any]]:
    return response.metadata.get("mcp_calls", [])


def _set_injected_ids(db: HubDatabase, session_id: str, ids: list[str]) -> None:
    SessionVariableManager(db).set_variable(session_id, "injected_memory_ids", ids)


def _get_injected_ids(db: HubDatabase, session_id: str) -> list[str]:
    return SessionVariableManager(db).get_variables(session_id).get("injected_memory_ids", [])


def _register_sessions(db: HubDatabase, *session_ids: str) -> None:
    project = LocalProjectManager(db).create(name=f"memory-helper-{time.monotonic_ns()}")
    for session_id in session_ids:
        db.execute(
            """
            INSERT INTO sessions (id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session_id, f"external-{session_id}", "machine-test", "claude", project.id),
        )


def _create_running_helper(
    db: HubDatabase,
    *,
    run_id: str,
    parent_session_id: str,
    child_session_id: str,
) -> Any:
    manager = LocalAgentRunManager(db)
    run = manager.create(
        parent_session_id=parent_session_id,
        provider="codex",
        prompt="memory helper prompt",
        agent_name="memory-recall-helper",
        child_session_id=child_session_id,
        run_id=run_id,
    )
    started = manager.start(run.id)
    assert started is not None
    return started


def _patch_rule_templates_to_external_id(
    engine: RuleEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacements = {
        CANCEL_RULE_NAME: [("parent_session_id", "{{ event.session_id }}")],
        DELIVER_RULE_NAME: [("target_session_id", "{{ event.session_id }}")],
        SPAWN_RULE_NAME: [
            ("parent_session_id", "{{ event.session_id }}"),
            ("prompt", None),
        ],
    }
    for rule_name, patches in replacements.items():
        row = engine.definition_manager.get_by_name(rule_name)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        arguments = body.effects[0].arguments
        for key, value in patches:
            if key == "prompt":
                prompt = str(arguments["prompt"])
                monkeypatch.setitem(
                    arguments,
                    "prompt",
                    prompt.replace(
                        "Parent session: {{ event.metadata.get('_platform_session_id') }}",
                        "Parent session: {{ event.session_id }}",
                    ),
                )
            else:
                monkeypatch.setitem(arguments, key, value)
        engine.definition_manager.update(row.id, definition_json=body.model_dump_json())


async def _stubbed_dispatcher(
    server: str,
    tool: str,
    args: dict[str, Any],
    event: Any,
) -> dict[str, Any]:
    del server, args, event
    if tool == "cancel_stale_helpers":
        return _envelope(tool, _cancel_payload())
    if tool == "deliver_pending_messages":
        return _envelope(tool, _deliver_payload())
    raise AssertionError(f"unexpected inline tool: {tool}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "variables"),
    [
        (
            _platform_event("Message from Gobby daemon: New activity available."),
            _base_variables(parent_turn_seq=7),
        ),
        (
            _platform_event(
                terminal_context={"agent_run_id": "run-child"},
            ),
            _base_variables(parent_turn_seq=7),
        ),
        (
            _platform_event(role="assistant"),
            _base_variables(parent_turn_seq=7),
        ),
        (
            _platform_event(),
            _base_variables(
                parent_turn_seq=7,
                _active_rule_names=[SPAWN_RULE_NAME],
            ),
        ),
    ],
)
async def test_spawn_memory_recall_helper_skips_ineligible_turns(
    db: HubDatabase,
    tmp_path: Path,
    event: HookEvent,
    variables: dict[str, Any],
) -> None:
    _sync_rules(db, tmp_path, [SPAWN_RULE_NAME])
    engine = RuleEngine(db, mcp_dispatcher=_stubbed_dispatcher)

    response = await engine.evaluate(event, session_id=PLATFORM_SESSION_ID, variables=variables)

    assert all(call["tool"] != "spawn_agent" for call in _mcp_calls(response))
    assert response.context is None


@pytest.mark.asyncio
async def test_spawn_memory_recall_helper_spawns_for_eligible_user_prompt(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    _sync_rules(db, tmp_path, [SPAWN_RULE_NAME])
    engine = RuleEngine(db, mcp_dispatcher=_stubbed_dispatcher)

    response = await engine.evaluate(
        _platform_event("please use memory to recall the relevant project convention"),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(
            parent_turn_seq=7,
            _active_rule_names=["memory-recall-on-prompt", SPAWN_RULE_NAME],
        ),
    )

    spawn_call = next(call for call in _mcp_calls(response) if call["tool"] == "spawn_agent")
    assert spawn_call["arguments"]["agent"] == "memory-recall-helper"
    assert spawn_call["arguments"]["parent_session_id"] == PLATFORM_SESSION_ID


@pytest.mark.asyncio
async def test_three_rule_session_id_sensitivity_integration(
    db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_rules(db, tmp_path, [CANCEL_RULE_NAME, DELIVER_RULE_NAME, SPAWN_RULE_NAME])
    inline_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def dispatcher(
        server: str, tool: str, args: dict[str, Any], event: Any
    ) -> dict[str, Any]:
        inline_calls.append((server, tool, args))
        return await _stubbed_dispatcher(server, tool, args, event)

    engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    response = await engine.evaluate(
        _platform_event(),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=7),
    )

    cancel_args = next(args for _, tool, args in inline_calls if tool == "cancel_stale_helpers")
    deliver_args = next(
        args for _, tool, args in inline_calls if tool == "deliver_pending_messages"
    )
    assert cancel_args["parent_session_id"] == PLATFORM_SESSION_ID
    assert cancel_args["parent_session_id"] != EXTERNAL_SESSION_ID
    assert deliver_args["target_session_id"] == PLATFORM_SESSION_ID
    assert deliver_args["target_session_id"] != EXTERNAL_SESSION_ID

    assert all(tool != "spawn_agent" for _, tool, _ in inline_calls)
    spawn_call = next(call for call in _mcp_calls(response) if call["tool"] == "spawn_agent")
    spawn_args = spawn_call["arguments"]
    assert spawn_args["agent"] == "memory-recall-helper"
    assert spawn_args["parent_session_id"] == PLATFORM_SESSION_ID
    assert spawn_args["parent_session_id"] != EXTERNAL_SESSION_ID
    assert "Parent session: platform-Y" in spawn_args["prompt"]
    assert "Parent session: external-X" not in spawn_args["prompt"]
    assert "origin_turn_seq: 7" in spawn_args["prompt"]

    _patch_rule_templates_to_external_id(engine, monkeypatch)
    patched_inline_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def patched_dispatcher(
        server: str,
        tool: str,
        args: dict[str, Any],
        event: Any,
    ) -> dict[str, Any]:
        patched_inline_calls.append((server, tool, args))
        return await _stubbed_dispatcher(server, tool, args, event)

    patched_engine = RuleEngine(db, mcp_dispatcher=patched_dispatcher)
    patched_response = await patched_engine.evaluate(
        _platform_event(),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=7),
    )

    patched_cancel = next(
        args for _, tool, args in patched_inline_calls if tool == "cancel_stale_helpers"
    )
    patched_deliver = next(
        args for _, tool, args in patched_inline_calls if tool == "deliver_pending_messages"
    )
    patched_spawn = next(
        call for call in _mcp_calls(patched_response) if call["tool"] == "spawn_agent"
    )["arguments"]

    assert patched_cancel["parent_session_id"] == EXTERNAL_SESSION_ID
    assert patched_deliver["target_session_id"] == EXTERNAL_SESSION_ID
    assert patched_spawn["parent_session_id"] == EXTERNAL_SESSION_ID
    assert "Parent session: external-X" in patched_spawn["prompt"]
    assert "Parent session: platform-Y" not in patched_spawn["prompt"]


@pytest.mark.asyncio
async def test_four_rule_turn_seq_and_freshness_integration(
    db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _sync_rules(
        db,
        tmp_path,
        [INCREMENT_RULE_NAME, CANCEL_RULE_NAME, DELIVER_RULE_NAME, SPAWN_RULE_NAME],
    )
    engine = RuleEngine(db, mcp_dispatcher=_stubbed_dispatcher)

    variables = _base_variables(parent_turn_seq=0)
    await engine.evaluate(_platform_event(), session_id=PLATFORM_SESSION_ID, variables=variables)
    assert variables["parent_turn_seq"] == 1

    turn_two = await engine.evaluate(
        _platform_event(),
        session_id=PLATFORM_SESSION_ID,
        variables=variables,
    )
    assert variables["parent_turn_seq"] == 2
    turn_two_spawn = next(call for call in _mcp_calls(turn_two) if call["tool"] == "spawn_agent")
    assert "origin_turn_seq: 2" in turn_two_spawn["arguments"]["prompt"]

    original_formatter = EffectsMixin._format_delivery_result
    formatter_calls: list[tuple[dict[str, Any], str | None, dict[str, Any]]] = []

    def spy_formatter(
        self: EffectsMixin,
        result: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
    ) -> str | None:
        formatter_calls.append((result, platform_session_id, dict(variables)))
        return original_formatter(self, result, platform_session_id, variables)

    monkeypatch.setattr(EffectsMixin, "_format_delivery_result", spy_formatter)

    fresh_message = _memory_recall_message(
        "mem-fresh",
        "content-sentinel-mem-fresh",
        origin_turn_seq=1,
        rationale="fresh",
    )

    async def fresh_dispatcher(
        server: str,
        tool: str,
        args: dict[str, Any],
        event: Any,
    ) -> dict[str, Any]:
        del server, args, event
        if tool == "cancel_stale_helpers":
            return _envelope(tool, _cancel_payload())
        if tool == "deliver_pending_messages":
            return _envelope(tool, _deliver_payload([fresh_message]))
        raise AssertionError(f"unexpected inline tool: {tool}")

    _set_injected_ids(db, PLATFORM_SESSION_ID, [])
    fresh_engine = RuleEngine(db, mcp_dispatcher=fresh_dispatcher)
    fresh_response = await fresh_engine.evaluate(
        _platform_event(),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=1),
    )

    assert len(formatter_calls) == 1
    assert formatter_calls[0][1] == PLATFORM_SESSION_ID
    assert "content-sentinel-mem-fresh" in _context(fresh_response)
    assert "mem-fresh" in _get_injected_ids(db, PLATFORM_SESSION_ID)

    stale_message = _memory_recall_message(
        "mem-stale",
        "content-sentinel-mem-stale",
        origin_turn_seq=0,
        rationale="stale",
    )

    async def stale_dispatcher(
        server: str,
        tool: str,
        args: dict[str, Any],
        event: Any,
    ) -> dict[str, Any]:
        del server, args, event
        if tool == "cancel_stale_helpers":
            return _envelope(tool, _cancel_payload())
        if tool == "deliver_pending_messages":
            return _envelope(tool, _deliver_payload([stale_message]))
        raise AssertionError(f"unexpected inline tool: {tool}")

    caplog.set_level("DEBUG", logger="gobby.workflows.engine.effects")
    _set_injected_ids(db, PLATFORM_SESSION_ID, [])
    stale_engine = RuleEngine(db, mcp_dispatcher=stale_dispatcher)
    stale_response = await stale_engine.evaluate(
        _platform_event(),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=1),
    )

    assert "content-sentinel-mem-stale" not in _context(stale_response)
    assert _get_injected_ids(db, PLATFORM_SESSION_ID) == []
    assert "Dropping stale memory_recall: origin=0" in caplog.text


@pytest.mark.asyncio
async def test_cancel_dispatches_before_deliver_inline(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    _sync_rules(db, tmp_path, [CANCEL_RULE_NAME, DELIVER_RULE_NAME])
    inline_calls: list[tuple[str, int, dict[str, Any]]] = []

    async def dispatcher(
        server: str, tool: str, args: dict[str, Any], event: Any
    ) -> dict[str, Any]:
        del server, event
        inline_calls.append((tool, time.monotonic_ns(), args))
        if tool == "cancel_stale_helpers":
            return _envelope(tool, _cancel_payload())
        if tool == "deliver_pending_messages":
            return _envelope(tool, _deliver_payload())
        raise AssertionError(f"unexpected inline tool: {tool}")

    engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    response = await engine.evaluate(
        _platform_event(prompt="short prompt allowed here"),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=2),
    )

    cancel = next(call for call in inline_calls if call[0] == "cancel_stale_helpers")
    deliver = next(call for call in inline_calls if call[0] == "deliver_pending_messages")
    assert cancel[1] < deliver[1]
    assert {call[0] for call in inline_calls} == {
        "cancel_stale_helpers",
        "deliver_pending_messages",
    }
    assert _mcp_calls(response) == []


@pytest.mark.asyncio
async def test_cancel_status_transitions_before_delivery_formatter_runs(
    db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_rules(db, tmp_path, [CANCEL_RULE_NAME, DELIVER_RULE_NAME])
    _register_sessions(db, PLATFORM_SESSION_ID, "child-stale")
    run = _create_running_helper(
        db,
        run_id="run-stale",
        parent_session_id=PLATFORM_SESSION_ID,
        child_session_id="child-stale",
    )
    InterSessionMessageManager(db).create_message(
        from_session="child-stale",
        to_session=PLATFORM_SESSION_ID,
        content=json.dumps(
            {
                "type": "memory_recall",
                "origin_turn_seq": 1,
                "memories": [
                    {"id": "mem-test4", "content": "content-sentinel-test4"},
                ],
                "rationale": "test4",
            }
        ),
    )

    run_manager = LocalAgentRunManager(db)
    runner = _DbBackedRunner(run_manager)
    session_manager = _IdentitySessionManager()
    registry = create_agents_registry(runner, db=db, session_manager=session_manager)
    add_messaging_tools(
        registry=registry,
        message_manager=InterSessionMessageManager(db),
        session_manager=session_manager,
        db=db,
    )

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.agents._kill_agent_process",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
        AsyncMock(return_value=None),
    )

    status_at_formatter: list[str] = []
    original_formatter = EffectsMixin._format_delivery_result

    def spy_formatter(
        self: EffectsMixin,
        result: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
    ) -> str | None:
        status_at_formatter.append(LocalAgentRunManager(db).get(run.id).status)
        return original_formatter(self, result, platform_session_id, variables)

    monkeypatch.setattr(EffectsMixin, "_format_delivery_result", spy_formatter)

    async def dispatcher(
        server: str, tool: str, args: dict[str, Any], event: Any
    ) -> dict[str, Any]:
        del event
        assert server == "gobby-agents"
        raw = await registry.call(tool, args)
        return {"success": raw.get("success", False), "inject_result": True, "result": raw}

    engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    _set_injected_ids(db, PLATFORM_SESSION_ID, [])
    response = await engine.evaluate(
        _platform_event(prompt="cancel before delivery has enough words"),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=2),
    )

    assert status_at_formatter == ["cancelled"]
    assert LocalAgentRunManager(db).get(run.id).status == "cancelled"
    assert "content-sentinel-test4" not in _context(response)
    assert "mem-test4" not in _get_injected_ids(db, PLATFORM_SESSION_ID)


@pytest.mark.asyncio
async def test_memory_helper_completion_is_silent_without_memory_recall(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    _sync_rules(db, tmp_path, [SPAWN_RULE_NAME, DELIVER_RULE_NAME])
    _register_sessions(db, PLATFORM_SESSION_ID, "child-silent", "child-memory")

    spawn_engine = RuleEngine(db)
    spawn_response = await spawn_engine.evaluate(
        _platform_event(prompt="six or more words for helper spawn rule"),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=3),
    )
    spawn_args = next(call for call in _mcp_calls(spawn_response) if call["tool"] == "spawn_agent")[
        "arguments"
    ]
    assert spawn_args["notify_parent_on_completion"] is False
    assert spawn_args["parent_session_id"] == PLATFORM_SESSION_ID

    message_manager = InterSessionMessageManager(db)
    wake_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def wake_callback(session_id: str, message: str, result: dict[str, Any]) -> None:
        wake_calls.append((session_id, message, result))

    completion_registry = CompletionEventRegistry(wake_callback=wake_callback)

    silent_runner = MagicMock()
    silent_runner.run_storage = MagicMock()
    silent_run = MagicMock(
        id="run-silent",
        child_session_id="child-silent",
        parent_session_id=PLATFORM_SESSION_ID,
        status="running",
        tmux_session_name=None,
    )
    silent_runner.run_storage.get_by_session.return_value = silent_run
    silent_runner.get_run.return_value = silent_run
    silent_runner.complete_run.return_value = True
    registry = create_agents_registry(silent_runner, completion_registry=completion_registry)

    from gobby.utils.session_context import session_context_for_test

    with (
        session_context_for_test("child-silent"),
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
    ):
        result = await registry._tools["end_agent_run"].func()

    assert result == {"success": True, "run_id": "run-silent", "status": "success"}
    assert (
        message_manager.list_messages(
            PLATFORM_SESSION_ID,
            direction="inbox",
            message_type="completion_notification",
        )
        == []
    )
    assert wake_calls == []

    message_manager.create_message(
        from_session="child-memory",
        to_session=PLATFORM_SESSION_ID,
        content=json.dumps(
            {
                "type": "memory_recall",
                "origin_turn_seq": 3,
                "memories": [
                    {"id": "mem-test5", "content": "content-sentinel-test5"},
                ],
                "rationale": "test5",
            }
        ),
    )

    memory_runner = MagicMock()
    memory_runner.run_storage = MagicMock()
    memory_run = MagicMock(
        id="run-memory",
        child_session_id="child-memory",
        parent_session_id=PLATFORM_SESSION_ID,
        status="running",
        tmux_session_name=None,
    )
    memory_runner.run_storage.get_by_session.return_value = memory_run
    memory_runner.get_run.return_value = memory_run
    memory_runner.complete_run.return_value = True
    memory_registry = create_agents_registry(
        memory_runner,
        completion_registry=completion_registry,
    )

    with (
        session_context_for_test("child-memory"),
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
    ):
        result = await memory_registry._tools["end_agent_run"].func()

    assert result == {"success": True, "run_id": "run-memory", "status": "success"}
    assert (
        message_manager.list_messages(
            PLATFORM_SESSION_ID,
            direction="inbox",
            message_type="completion_notification",
        )
        == []
    )
    assert wake_calls == []

    messaging_registry = InternalToolRegistry("gobby-agents")
    add_messaging_tools(
        registry=messaging_registry,
        message_manager=message_manager,
        session_manager=_IdentitySessionManager(),
        db=db,
    )

    async def dispatcher(
        server: str, tool: str, args: dict[str, Any], event: Any
    ) -> dict[str, Any]:
        del server, event
        raw = await messaging_registry.call(tool, args)
        return {"success": raw.get("success", False), "inject_result": True, "result": raw}

    delivery_engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    _set_injected_ids(db, PLATFORM_SESSION_ID, [])
    delivered = await delivery_engine.evaluate(
        _platform_event(prompt="next parent turn consumes explicit memory recall"),
        session_id=PLATFORM_SESSION_ID,
        variables=_base_variables(parent_turn_seq=4),
    )

    rendered = _context(delivered)
    assert "content-sentinel-test5" in rendered
    assert "completion_notification" not in rendered
    assert "Agent run" not in rendered
    assert "completed" not in rendered
