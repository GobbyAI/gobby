"""Concurrent hook admission regressions against the isolated test hub."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.adapter_execution import run_adapter_hook
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime

pytestmark = pytest.mark.unit

_CODEX_SESSIONS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)
_GROK_SESSION = "33333333-3333-4333-8333-333333333333"


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
                )
            ],
        ).model_dump(),
        priority=10,
        enabled=True,
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
async def test_nonblocking_inline_mcp_result_is_delivered_on_next_hook(
    temp_db: HubDatabase,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    _insert_optional_mcp_rule(
        manager,
        name="surface-memory-without-blocking",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="event.data.get('tool_name') == 'SlowTool'",
        server="gobby-memory",
        tool="surface_memories",
        arguments={"text": "remember hook admission", "trigger": "turn"},
    )
    dispatch_finished = asyncio.Event()

    async def dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        dispatch_finished.set()
        return {
            "success": True,
            "result": {
                "count": 1,
                "trigger": "turn",
                "memories": [
                    {
                        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "type": "fact",
                        "search_via": "semantic|keyword",
                        "updated_at": "2026-09-22T12:00:00Z",
                        "content": "Hook admission must stay responsive.",
                        "rationale": "Use when changing hook concurrency.",
                    }
                ],
            },
        }

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    variables = {"project": {"id": "isolated-hub", "path": "/tmp"}}
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
        variables=variables,
    )
    await asyncio.wait_for(dispatch_finished.wait(), timeout=1.0)
    second = await engine.evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=_CODEX_SESSIONS[0],
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "FastTool"},
            metadata={"_platform_session_id": _CODEX_SESSIONS[0]},
        ),
        session_id=_CODEX_SESSIONS[0],
        variables=variables,
    )

    assert first.context is None
    assert second.context is not None
    assert '<memory-index trigger="turn">' in second.context
    assert "Hook admission must stay responsive." in second.context


@pytest.mark.asyncio
async def test_nonblocking_mcp_timeout_does_not_extend_hook_completion(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    _insert_optional_mcp_rule(
        manager,
        name="surface-memory-timeout",
        event=RuleTriggerEvent.BEFORE_TOOL,
        when="True",
        server="gobby-memory",
        tool="surface_memories",
        arguments={"text": "timeout probe", "trigger": "turn"},
        timeout_seconds=0.05,
    )
    dispatch_cancelled = asyncio.Event()

    async def stalled_dispatcher(
        _server: str,
        _tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
            return {"success": True, "result": {}}
        finally:
            dispatch_cancelled.set()

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
        await asyncio.wait_for(dispatch_cancelled.wait(), timeout=1.0)

    assert response.decision == "allow"
    assert hook_seconds < 0.2
    assert "timed out after 0.05s (rule surface-memory-timeout)" in caplog.text


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

    dispatch_started_count = 0
    dispatch_finished_count = 0
    dispatch_count_lock = threading.Lock()
    all_dispatches_started = threading.Event()
    all_dispatches_finished = threading.Event()
    release_dispatches = threading.Event()

    async def slow_dispatcher(
        _server: str,
        tool: str,
        _arguments: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        nonlocal dispatch_started_count, dispatch_finished_count
        with dispatch_count_lock:
            dispatch_started_count += 1
            if dispatch_started_count == 6:
                all_dispatches_started.set()
        await asyncio.to_thread(release_dispatches.wait)
        with dispatch_count_lock:
            dispatch_finished_count += 1
            if dispatch_finished_count == 6:
                all_dispatches_finished.set()
        if tool == "recall_review_lessons_for_files":
            return {"success": True, "result": {"count": 0, "lessons": []}}
        return {"success": True, "result": {"count": 0, "memories": [], "trigger": "turn"}}

    engine = RuleEngine(temp_db, mcp_dispatcher=slow_dispatcher)
    runtime = WorkflowEvaluationRuntime(max_workers=8)
    adapter = _adapter_for_engine(engine, runtime)
    hook_specs = [
        (session_id, "codex", event_type)
        for session_id in _CODEX_SESSIONS
        for event_type in ("before_tool", "after_tool")
    ] + [(_GROK_SESSION, "grok", event_type) for event_type in ("before_tool", "after_tool")]
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
        assert await asyncio.to_thread(all_dispatches_started.wait, 1.0)
        release_dispatches.set()
        assert await asyncio.to_thread(all_dispatches_finished.wait, 3.0)
    finally:
        release_dispatches.set()
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
    assert len(durations) == 6
    assert p95_seconds < 1.0
    assert all(
        latency_ms < 1_000 for values in measured_rule_timings.values() for latency_ms in values
    )
