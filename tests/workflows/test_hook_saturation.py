"""Concurrent hook admission regressions against the isolated test hub."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.adapter_execution import run_adapter_hook
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.phase_timing import HOOK_PHASES, HookPhaseTimings, hook_phase_timing_scope
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

pytestmark = pytest.mark.unit

_CODEX_SESSIONS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)
_GROK_SESSION = "33333333-3333-4333-8333-333333333333"


class _FakeLoopClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _insert_optional_mcp_rule(
    manager: RuleDefinitionManager,
    *,
    name: str,
    event: RuleTriggerEvent,
    when: str,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 2.0,
    success_variable: str | None = None,
    delivery: Literal["eager", "on_receipt"] = "eager",
    tags: list[str] | None = None,
) -> None:
    manager.create(
        name=name,
        definition_json=RuleDefinitionBody(
            event=event,
            when=when,
            effects=[
                RuleEffect(
                    type="mcp_call",
                    server=server,
                    tool=tool,
                    arguments=arguments,
                    inject_result=True,
                    timeout_seconds=timeout_seconds,
                    success_variable=success_variable,
                    delivery=delivery,
                )
            ],
        ).model_dump(),
        priority=10,
        enabled=True,
        tags=tags,
    )


def _adapter_for_engine(
    engine: RuleEngine,
    runtime: WorkflowEvaluationRuntime,
) -> MagicMock:
    adapter = MagicMock()

    def handle_native(payload: dict[str, Any], _hook_manager: object) -> dict[str, bool]:
        event = HookEvent(
            event_type=HookEventType(payload["event_type"]),
            session_id=payload["_platform_session_id"],
            source=SessionSource(payload["source"]),
            timestamp=datetime.now(UTC),
            data={
                "tool_name": payload["tool_name"],
                "provider": payload["source"],
            },
            metadata={"_platform_session_id": payload["_platform_session_id"]},
        )
        response = runtime.run(
            engine.evaluate(
                event,
                session_id=payload["_platform_session_id"],
                variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
            ),
            timeout=4.0,
        )
        return {"continue": response.decision == "allow"}

    adapter.handle_native.side_effect = handle_native
    return adapter


@pytest.mark.asyncio
async def test_slow_rule_evaluation_does_not_hold_admission_for_unrelated_hooks(
    temp_db: HubDatabase,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    _insert_optional_mcp_rule(
        manager,
        name="slow-optional-memory-rule",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="event.data.get('tool_name') == 'SlowTool'",
        server="gobby-memory",
        tool="surface_memories",
        arguments={"text": "slow admission probe", "trigger": "turn"},
    )
    dispatch_started = threading.Event()
    release_dispatch = threading.Event()
    dispatch_finished = threading.Event()

    async def slow_dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        dispatch_started.set()
        try:
            await asyncio.to_thread(release_dispatch.wait)
            return {"success": True, "result": {"memories": [], "trigger": "turn"}}
        finally:
            dispatch_finished.set()

    engine = RuleEngine(temp_db, mcp_dispatcher=slow_dispatcher)
    runtime = WorkflowEvaluationRuntime(max_workers=4)
    adapter = _adapter_for_engine(engine, runtime)
    slow_hook = asyncio.create_task(
        run_adapter_hook(
            adapter,
            {
                "_platform_session_id": _CODEX_SESSIONS[0],
                "event_type": "before_tool",
                "source": "codex",
                "tool_name": "SlowTool",
            },
            MagicMock(),
            timeout_seconds=3.0,
        )
    )
    unrelated_hook: asyncio.Task[dict[str, Any]] | None = None
    try:
        assert await asyncio.to_thread(dispatch_started.wait, 1.0)
        unrelated_hook = asyncio.create_task(
            run_adapter_hook(
                adapter,
                {
                    "_platform_session_id": _CODEX_SESSIONS[0],
                    "event_type": "before_tool",
                    "source": "codex",
                    "tool_name": "FastTool",
                },
                MagicMock(),
                timeout_seconds=3.0,
            )
        )

        response = await asyncio.wait_for(unrelated_hook, timeout=0.2)

        assert response == {"continue": True}
    finally:
        release_dispatch.set()
        pending = [slow_hook]
        if unrelated_hook is not None:
            pending.append(unrelated_hook)
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.to_thread(dispatch_finished.wait, 1.0)
        runtime.shutdown()


@pytest.mark.asyncio
async def test_cached_mcp_result_serves_matching_hook_inline_without_redispatch(
    temp_db: HubDatabase,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    _insert_optional_mcp_rule(
        manager,
        name="surface-memory-without-blocking",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="event.data.get('tool_name') == 'SlowTool'",
        server="gobby-skills",
        tool="list_hubs",
        arguments={},
    )
    dispatch_count = 0

    async def dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        nonlocal dispatch_count
        dispatch_count += 1
        return {
            "success": True,
            "result": {"hubs": [{"name": "cached-hub", "type": "test"}]},
        }

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    first = await engine.evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=_CODEX_SESSIONS[0],
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "SlowTool"},
            metadata={"_platform_session_id": _CODEX_SESSIONS[0]},
        ),
        session_id=_CODEX_SESSIONS[0],
        variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
    )
    second = await engine.evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=_CODEX_SESSIONS[0],
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "SlowTool"},
            metadata={"_platform_session_id": _CODEX_SESSIONS[0]},
        ),
        session_id=_CODEX_SESSIONS[0],
        variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
    )

    assert dispatch_count == 1
    assert first.context is not None
    assert "cached-hub" in first.context
    assert second.context is not None
    assert "cached-hub" in second.context


@pytest.mark.asyncio
async def test_cached_mcp_result_expires_from_fill_time_without_hit_extension(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    arguments = {"file_paths_json": '["src/example.py"]', "limit": 3}
    _insert_optional_mcp_rule(
        manager,
        name="recall-review-lessons-with-expiry",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="True",
        server="gobby-review-learning",
        tool="recall_review_lessons_for_files",
        arguments=arguments,
    )
    clock = _FakeLoopClock(1_000.0)
    monkeypatch.setattr(asyncio.get_running_loop(), "time", clock)
    lesson: dict[str, str] | None = None
    dispatch_count = 0

    async def dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        nonlocal dispatch_count
        dispatch_count += 1
        lessons = [lesson] if lesson is not None else []
        return {"success": True, "result": {"count": len(lessons), "lessons": lessons}}

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)

    async def evaluate() -> Any:
        return await engine.evaluate(
            HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=_CODEX_SESSIONS[0],
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data={"tool_name": "Edit"},
                metadata={"_platform_session_id": _CODEX_SESSIONS[0]},
            ),
            session_id=_CODEX_SESSIONS[0],
            variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
        )

    initial = await evaluate()
    lesson = {
        "memory_id": "lesson-after-fill",
        "pattern_id": "new-lesson-after-fill",
        "matched_file_path": "src/example.py",
        "do": "Refresh cached review guidance.",
        "avoid": "Serving expired review guidance.",
    }
    clock.advance(60.0)
    first_hit = await evaluate()
    clock.advance(59.0)
    second_hit = await evaluate()
    clock.advance(2.0)
    refreshed = await evaluate()

    assert dispatch_count == 2
    assert initial.context is None
    assert first_hit.context is None
    assert second_hit.context is None
    assert refreshed.context is not None
    assert "new-lesson-after-fill" in refreshed.context


@pytest.mark.asyncio
async def test_mcp_timeout_stays_capped_and_background_result_fills_cache(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    _insert_optional_mcp_rule(
        manager,
        name="surface-memory-timeout",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="True",
        server="gobby-skills",
        tool="list_hubs",
        arguments={},
        timeout_seconds=0.05,
    )
    dispatch_count = 0
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    dispatch_finished = asyncio.Event()

    async def stalled_dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        nonlocal dispatch_count
        dispatch_count += 1
        dispatch_started.set()
        try:
            await release_dispatch.wait()
            return {
                "success": True,
                "result": {"hubs": [{"name": "late-hub", "type": "test"}]},
            }
        finally:
            dispatch_finished.set()

    engine = RuleEngine(temp_db, mcp_dispatcher=stalled_dispatcher)
    started_at = time.perf_counter()
    with caplog.at_level(logging.WARNING):
        response = await engine.evaluate(
            HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=_CODEX_SESSIONS[0],
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data={"tool_name": "Read"},
                metadata={"_platform_session_id": _CODEX_SESSIONS[0]},
            ),
            session_id=_CODEX_SESSIONS[0],
            variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
        )
        hook_seconds = time.perf_counter() - started_at
        await asyncio.wait_for(dispatch_started.wait(), timeout=1.0)
        release_dispatch.set()
        await asyncio.wait_for(dispatch_finished.wait(), timeout=1.0)
        cached_started_at = time.perf_counter()
        cached_response = await engine.evaluate(
            HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=_CODEX_SESSIONS[0],
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data={"tool_name": "Read"},
                metadata={"_platform_session_id": _CODEX_SESSIONS[0]},
            ),
            session_id=_CODEX_SESSIONS[0],
            variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
        )
        cached_seconds = time.perf_counter() - cached_started_at

    assert response.decision == "allow"
    assert response.context is None
    assert hook_seconds < 0.2
    assert dispatch_count == 1
    assert cached_seconds < 0.2
    assert cached_response.context is not None
    assert "late-hub" in cached_response.context
    assert "timed out after 0.05s (rule surface-memory-timeout)" in caplog.text


_CLAUDE_SESSIONS = (
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
)
_LATE_RECALL_BODY = "Late recall body that must reach the model."


class _StalledRecall:
    """surface_memories dispatcher that holds every call until released."""

    def __init__(self, expected_calls: int = 1) -> None:
        self.calls = 0
        self.release = asyncio.Event()
        self._expected_calls = expected_calls
        self._finished_calls = 0
        self._all_finished = asyncio.Event()

    async def __call__(
        self,
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        self.calls += 1
        try:
            await self.release.wait()
            return {
                "success": True,
                "result": {
                    "trigger": "turn",
                    "memories": [
                        {
                            "id": "latemem1-0000-4000-8000-000000000000",
                            "type": "fact",
                            "content": _LATE_RECALL_BODY,
                        }
                    ],
                },
            }
        finally:
            self._finished_calls += 1
            if self._finished_calls >= self._expected_calls:
                self._all_finished.set()

    async def release_and_fill(self) -> None:
        # The background task caches in the same step that returns from the
        # dispatcher, so the fill is visible once every call has finished.
        self.release.set()
        await asyncio.wait_for(self._all_finished.wait(), timeout=1.0)


def _insert_turn_start_recall_rule(manager: RuleDefinitionManager) -> None:
    _insert_optional_mcp_rule(
        manager,
        name="surface-memories-late",
        event=RuleTriggerEvent.TURN_START,
        when="True",
        server="gobby-memory",
        tool="surface_memories",
        arguments={"text": "recall please", "trigger": "turn"},
        timeout_seconds=0.05,
    )


async def _evaluate_claude_hook(
    engine: RuleEngine,
    session_id: str,
    event_type: HookEventType,
    native_hook_type: str,
    data: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> Any:
    return await engine.evaluate(
        HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data=data or {},
            metadata={
                "_platform_session_id": session_id,
                "_native_hook_type": native_hook_type,
            },
        ),
        session_id=session_id,
        variables=variables or {"project": {"id": "isolated-hub", "path": "/tmp"}},
    )


async def _prompt(engine: RuleEngine, session_id: str) -> Any:
    return await _evaluate_claude_hook(
        engine,
        session_id,
        HookEventType.BEFORE_AGENT,
        "user-prompt-submit",
        {"prompt": "recall please"},
    )


async def _pre_tool(
    engine: RuleEngine,
    session_id: str,
    variables: dict[str, Any] | None = None,
) -> Any:
    return await _evaluate_claude_hook(
        engine,
        session_id,
        HookEventType.BEFORE_TOOL,
        "pre-tool-use",
        {"tool_name": "Read"},
        variables,
    )


def _late_recall_count(response: Any) -> int:
    return str(response.context or "").count(_LATE_RECALL_BODY)


@pytest.mark.asyncio
async def test_timed_out_recall_is_delivered_once_on_the_next_context_hook(
    temp_db: HubDatabase,
) -> None:
    _insert_turn_start_recall_rule(RuleDefinitionManager(temp_db))
    recall = _StalledRecall()
    engine = RuleEngine(temp_db, mcp_dispatcher=recall)
    session_id = _CLAUDE_SESSIONS[0]

    timed_out = await _prompt(engine, session_id)
    await recall.release_and_fill()
    # Claude's Stop output carries no model context, so the recall waits.
    stop = await _evaluate_claude_hook(engine, session_id, HookEventType.STOP, "stop")
    delivered = await _pre_tool(engine, session_id)
    following = await _pre_tool(engine, session_id)

    assert recall.calls == 1
    assert timed_out.decision == "allow"
    assert _late_recall_count(timed_out) == 0
    assert _late_recall_count(stop) == 0
    assert _late_recall_count(delivered) == 1
    assert _late_recall_count(following) == 0


@pytest.mark.asyncio
async def test_timed_out_recall_refired_after_fill_is_delivered_once(
    temp_db: HubDatabase,
) -> None:
    _insert_turn_start_recall_rule(RuleDefinitionManager(temp_db))
    recall = _StalledRecall()
    engine = RuleEngine(temp_db, mcp_dispatcher=recall)
    session_id = _CLAUDE_SESSIONS[0]

    timed_out = await _prompt(engine, session_id)
    await recall.release_and_fill()
    # The same call key fires again: the cache hit and the late copy are one delivery.
    refired = await _prompt(engine, session_id)
    following = await _pre_tool(engine, session_id)

    assert recall.calls == 1
    assert _late_recall_count(timed_out) == 0
    assert _late_recall_count(refired) == 1
    assert _late_recall_count(following) == 0


@pytest.mark.asyncio
async def test_late_delivery_sets_the_rule_success_variable(
    temp_db: HubDatabase,
) -> None:
    # Mirrors list-skill-hubs-once-per-session: without the success variable the
    # rule fires again next turn and the model gets the same result twice.
    _insert_optional_mcp_rule(
        RuleDefinitionManager(temp_db),
        name="surface-memories-late-once",
        event=RuleTriggerEvent.TURN_START,
        when="not variables.get('late_recall_shown')",
        server="gobby-memory",
        tool="surface_memories",
        arguments={"text": "recall please", "trigger": "turn"},
        timeout_seconds=0.05,
        success_variable="late_recall_shown",
        delivery="on_receipt",
    )
    recall = _StalledRecall()
    engine = RuleEngine(temp_db, mcp_dispatcher=recall)
    session_id = _CLAUDE_SESSIONS[0]
    delivering_variables: dict[str, Any] = {"project": {"id": "isolated-hub", "path": "/tmp"}}

    timed_out = await _prompt(engine, session_id)
    await recall.release_and_fill()
    delivered = await _pre_tool(engine, session_id, delivering_variables)

    assert STAGED_EFFECTS_FIELD not in timed_out.metadata
    assert _late_recall_count(delivered) == 1
    assert delivering_variables["late_recall_shown"] is True
    assert delivered.metadata[STAGED_EFFECTS_FIELD]["session_variables"] == {
        "late_recall_shown": True
    }


@pytest.mark.asyncio
async def test_timed_out_recall_is_dropped_after_cache_ttl(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_turn_start_recall_rule(RuleDefinitionManager(temp_db))
    recall = _StalledRecall(expected_calls=len(_CLAUDE_SESSIONS))
    engine = RuleEngine(temp_db, mcp_dispatcher=recall)
    for session_id in _CLAUDE_SESSIONS:
        await _prompt(engine, session_id)
    await recall.release_and_fill()
    loop = asyncio.get_running_loop()
    clock = _FakeLoopClock(loop.time())
    monkeypatch.setattr(loop, "time", clock)

    clock.advance(119.0)
    inside_ttl = await _pre_tool(engine, _CLAUDE_SESSIONS[0])
    clock.advance(2.0)
    expired = await _pre_tool(engine, _CLAUDE_SESSIONS[1])
    following = await _pre_tool(engine, _CLAUDE_SESSIONS[1])

    assert recall.calls == len(_CLAUDE_SESSIONS)
    assert _late_recall_count(inside_ttl) == 1
    assert _late_recall_count(expired) == 0
    assert _late_recall_count(following) == 0


@pytest.mark.asyncio
async def test_rule_rows_load_once_per_rules_revision(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        return {"success": True, "result": {"hubs": [{"name": "fresh-hub", "type": "test"}]}}

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    loads = 0
    list_by_event = engine.rule_manager.list_by_event

    def counted_list_by_event(*args: Any, **kwargs: Any) -> Any:
        nonlocal loads
        loads += 1
        return list_by_event(*args, **kwargs)

    monkeypatch.setattr(engine.rule_manager, "list_by_event", counted_list_by_event)
    session_id = _CLAUDE_SESSIONS[0]

    await _pre_tool(engine, session_id)
    loads_after_first_hook = loads
    await _pre_tool(engine, session_id)
    loads_after_repeat_hook = loads
    # A committed rule write bumps the rules revision, so the next hook reloads.
    _insert_optional_mcp_rule(
        RuleDefinitionManager(temp_db),
        name="fresh-rule-after-cache",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="True",
        server="gobby-skills",
        tool="list_hubs",
        arguments={},
    )
    after_write = await _pre_tool(engine, session_id)

    assert loads_after_first_hook > 0
    assert loads_after_repeat_hook == loads_after_first_hook
    assert loads > loads_after_repeat_hook
    assert "fresh-hub" in str(after_write.context or "")


@pytest.mark.asyncio
async def test_inflight_mcp_result_fans_out_to_distinct_consumers(
    temp_db: HubDatabase,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    shared_arguments = {"query": "shared"}
    _insert_optional_mcp_rule(
        manager,
        name="external-on-receipt-consumer",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="event.data.get('tool_name') == 'ExternalTool'",
        server="gobby-skills",
        tool="list_hubs",
        arguments=shared_arguments,
        success_variable="external_consumer_succeeded",
        delivery="on_receipt",
        tags=["user"],
    )
    _insert_optional_mcp_rule(
        manager,
        name="internal-reserved-consumer",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="event.data.get('tool_name') == 'InternalTool'",
        server="gobby-skills",
        tool="list_hubs",
        arguments=shared_arguments,
        success_variable="listed_servers",
        tags=["gobby"],
    )
    _insert_optional_mcp_rule(
        manager,
        name="external-reserved-consumer",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="event.data.get('tool_name') == 'UntrustedTool'",
        server="gobby-skills",
        tool="list_hubs",
        arguments=shared_arguments,
        success_variable="listed_servers",
        tags=["user"],
    )
    dispatch_count = 0
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        nonlocal dispatch_count
        dispatch_count += 1
        dispatch_started.set()
        await release_dispatch.wait()
        return {
            "success": True,
            "result": {"hubs": [{"name": "shared-hub", "type": "test"}]},
        }

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    external_variables: dict[str, Any] = {"project": {"id": "isolated-hub", "path": "/tmp"}}
    internal_variables: dict[str, Any] = {"project": {"id": "isolated-hub", "path": "/tmp"}}
    untrusted_variables: dict[str, Any] = {"project": {"id": "isolated-hub", "path": "/tmp"}}
    evaluations_entered = 0
    all_evaluations_entered = asyncio.Event()

    async def evaluate(tool_name: str, variables: dict[str, Any]) -> Any:
        nonlocal evaluations_entered
        evaluations_entered += 1
        if evaluations_entered == 3:
            all_evaluations_entered.set()
        await all_evaluations_entered.wait()
        return await engine.evaluate(
            HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=_CODEX_SESSIONS[0],
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data={"tool_name": tool_name},
                metadata={"_platform_session_id": _CODEX_SESSIONS[0]},
            ),
            session_id=_CODEX_SESSIONS[0],
            variables=variables,
        )

    evaluations = [
        asyncio.create_task(evaluate("ExternalTool", external_variables)),
        asyncio.create_task(evaluate("InternalTool", internal_variables)),
        asyncio.create_task(evaluate("UntrustedTool", untrusted_variables)),
    ]
    await asyncio.wait_for(dispatch_started.wait(), timeout=1.0)
    release_dispatch.set()
    responses = await asyncio.gather(*evaluations)

    assert dispatch_count == 1
    assert all(response.context and "shared-hub" in response.context for response in responses)
    assert external_variables["external_consumer_succeeded"] is True
    assert responses[0].metadata[STAGED_EFFECTS_FIELD]["session_variables"] == {
        "external_consumer_succeeded": True
    }
    assert internal_variables["listed_servers"] is True
    assert "listed_servers" not in untrusted_variables


@pytest.mark.asyncio
async def test_cached_mcp_result_is_not_delivered_across_sessions(
    temp_db: HubDatabase,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    _insert_optional_mcp_rule(
        manager,
        name="session-scoped-cache",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="True",
        server="gobby-skills",
        tool="list_hubs",
        arguments={},
    )
    dispatched_sessions: list[str] = []

    async def dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        event: HookEvent,
    ) -> dict[str, Any]:
        session_id = event.metadata["_platform_session_id"]
        dispatched_sessions.append(session_id)
        return {
            "success": True,
            "result": {"hubs": [{"name": session_id, "type": "test"}]},
        }

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)

    async def evaluate(session_id: str) -> Any:
        return await engine.evaluate(
            HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=session_id,
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data={"tool_name": "Read"},
                metadata={"_platform_session_id": session_id},
            ),
            session_id=session_id,
            variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
        )

    first = await evaluate(_CODEX_SESSIONS[0])
    second = await evaluate(_CODEX_SESSIONS[1])

    assert dispatched_sessions == list(_CODEX_SESSIONS)
    assert first.context is not None
    assert _CODEX_SESSIONS[0] in first.context
    assert _CODEX_SESSIONS[1] not in first.context
    assert second.context is not None
    assert _CODEX_SESSIONS[1] in second.context
    assert _CODEX_SESSIONS[0] not in second.context


@pytest.mark.asyncio
async def test_isolated_hub_two_codex_one_grok_hook_p95_under_one_second(
    temp_db: HubDatabase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = RuleDefinitionManager(temp_db)
    rule_specs = (
        (
            "codex-pretool-recall-review-lessons",
            RuleTriggerEvent.BEFORE_TOOL,
            "codex",
            "gobby-review-learning",
            "recall_review_lessons_for_files",
            {"file_paths_json": '["src/example.py"]', "limit": 3},
        ),
        (
            "codex-posttool-surface-memories",
            RuleTriggerEvent.AFTER_TOOL,
            "codex",
            "gobby-memory",
            "surface_memories",
            {"text": "codex post-tool", "trigger": "turn"},
        ),
        (
            "grok-pretool-recall-review-lessons",
            RuleTriggerEvent.BEFORE_TOOL,
            "grok",
            "gobby-review-learning",
            "recall_review_lessons_for_files",
            {"file_paths_json": '["src/example.py"]', "limit": 3},
        ),
        (
            "grok-posttool-surface-memories",
            RuleTriggerEvent.AFTER_TOOL,
            "grok",
            "gobby-memory",
            "surface_memories",
            {"text": "grok post-tool", "trigger": "turn"},
        ),
    )
    for name, event, provider, server, tool, arguments in rule_specs:
        _insert_optional_mcp_rule(
            manager,
            name=name,
            event=event,
            when=f"event.data.get('provider') == '{provider}'",
            server=server,
            tool=tool,
            arguments=arguments,
        )

    dispatch_count = 0
    dispatch_count_lock = threading.Lock()

    async def dispatcher(
        _server: str,
        tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        nonlocal dispatch_count
        with dispatch_count_lock:
            dispatch_count += 1
        if tool == "recall_review_lessons_for_files":
            return {"success": True, "result": {"count": 0, "lessons": []}}
        return {"success": True, "result": {"count": 0, "memories": [], "trigger": "turn"}}

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    hook_specs = [
        (session_id, "codex", event_type)
        for session_id in _CODEX_SESSIONS
        for event_type in ("before_tool", "after_tool")
    ] + [(_GROK_SESSION, "grok", event_type) for event_type in ("before_tool", "after_tool")]
    for session_id, source, event_type in hook_specs:
        await engine.evaluate(
            HookEvent(
                event_type=HookEventType(event_type),
                session_id=session_id,
                source=SessionSource(source),
                timestamp=datetime.now(UTC),
                data={
                    "provider": source,
                    "tool_name": "Edit" if event_type == "before_tool" else "EditResult",
                },
                metadata={"_platform_session_id": session_id},
            ),
            session_id=session_id,
            variables={"project": {"id": "isolated-hub", "path": "/tmp"}},
        )
    assert dispatch_count == 6

    runtime = WorkflowEvaluationRuntime(max_workers=8)
    adapter = _adapter_for_engine(engine, runtime)
    started_at = time.perf_counter()
    completed_at: dict[tuple[str, str, str], float] = {}
    rule_timings_ms: dict[str, list[float]] = {}
    timings_lock = threading.Lock()

    def capture_rule_timing(
        *,
        rule_name: str,
        latency_ms: float,
        **_kwargs: Any,
    ) -> None:
        with timings_lock:
            rule_timings_ms.setdefault(rule_name, []).append(latency_ms)

    async def execute_hook(
        session_id: str,
        source: str,
        event_type: str,
    ) -> dict[str, Any]:
        result = await run_adapter_hook(
            adapter,
            {
                "_platform_session_id": session_id,
                "event_type": event_type,
                "source": source,
                "tool_name": "Edit" if event_type == "before_tool" else "EditResult",
            },
            MagicMock(),
            timeout_seconds=4.0,
        )
        completed_at[(session_id, source, event_type)] = time.perf_counter() - started_at
        return result

    try:
        with patch(
            "gobby.workflows.engine.evaluation.record_rule_evaluation",
            side_effect=capture_rule_timing,
        ):
            responses = await asyncio.gather(
                *(execute_hook(*hook_spec) for hook_spec in hook_specs)
            )
    finally:
        runtime.shutdown()

    durations = sorted(completed_at.values())
    p95_seconds = durations[math.ceil(len(durations) * 0.95) - 1]
    measured_rule_timings = {
        name: [round(value, 3) for value in values]
        for name, values in sorted(rule_timings_ms.items())
        if name in {spec[0] for spec in rule_specs}
    }
    report = {
        "hook_seconds": {
            f"{source}:{event_type}:{session_id[:8]}": round(duration, 4)
            for (session_id, source, event_type), duration in sorted(completed_at.items())
        },
        "p95_seconds": round(p95_seconds, 4),
        "rule_timings_ms": measured_rule_timings,
    }
    with capsys.disabled():
        print(f"hook-saturation-measurement={report}")

    assert all(response == {"continue": True} for response in responses)
    assert dispatch_count == 6
    assert len(durations) == 6
    assert p95_seconds < 1.0
    assert all(
        latency_ms < 1_000 for values in measured_rule_timings.values() for latency_ms in values
    )


def test_rule_evaluation_breakdown_names_each_subphase_and_session(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #22708 criterion 7: a slow rule_evaluation phase must say where its time went, and
    # which session, so each slow hook joins its rule-allow-audit rows.
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="rule-breakdown", repo_path=None)
    session_id = SessionManager(temp_db).register_session(
        external_id="rule-breakdown",
        machine_id=machine_id,
        source="claude",
        project_id=project.id,
    )
    assert session_id
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read"},
        metadata={"_platform_session_id": session_id},
        cwd=str(tmp_path),
    )
    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        evaluation_runtime=WorkflowEvaluationRuntime(max_workers=2),
    )
    timings = HookPhaseTimings()
    try:
        with hook_phase_timing_scope(timings):
            response = handler.evaluate(event)
    finally:
        handler.shutdown()

    assert response.decision == "allow"
    assert set(timings.breakdown()) >= {
        "rule_runtime_queue",
        "rule_eval_lock_wait",
        "rule_prelude",
        "rule_engine",
        "rule_engine_db_reads",
    }
    assert set(timings.snapshot()) == set(HOOK_PHASES)
    assert timings.session_id == session_id
