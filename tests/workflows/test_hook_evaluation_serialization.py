from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.integration

EvalFn = Callable[..., Awaitable[HookResponse]]


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "hook_eval_serialization.db")
    run_migrations(database)
    return database


def _event(
    event_type: HookEventType,
    *,
    session_id: str = "external-session",
    platform_session_id: str = "platform-session",
    data: dict[str, Any] | None = None,
    cwd: str,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata={"_platform_session_id": platform_session_id},
        cwd=cwd,
    )


def _handler_with_fake_engine(evaluate: EvalFn) -> WorkflowHookHandler:
    rule_engine = MagicMock()
    rule_engine.db = MagicMock()
    rule_engine.evaluate = evaluate

    session_vars = MagicMock()
    session_vars.get_variables.return_value = {
        "baseline_dirty_files": [],
        "session_edited_files": [],
    }

    handler = WorkflowHookHandler(loop=None)
    handler.rule_engine = rule_engine
    handler._session_var_manager = session_vars
    return handler


@pytest.mark.asyncio
async def test_same_session_evaluations_are_serialized(tmp_path) -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    entered: list[str] = []

    async def evaluate(
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
    ) -> HookResponse:
        del session_id, variables, eval_context
        name = str(event.data["name"])
        entered.append(name)
        if name == "first":
            first_entered.set()
            await release_first.wait()
        return HookResponse(decision="allow")

    handler = _handler_with_fake_engine(evaluate)
    first = asyncio.create_task(
        handler._evaluate_rules(
            _event(
                HookEventType.BEFORE_TOOL,
                data={"name": "first"},
                cwd=str(tmp_path),
            )
        )
    )
    await asyncio.wait_for(first_entered.wait(), timeout=1)

    second = asyncio.create_task(
        handler._evaluate_rules(
            _event(
                HookEventType.BEFORE_TOOL,
                data={"name": "second"},
                cwd=str(tmp_path),
            )
        )
    )
    await asyncio.sleep(0.05)

    assert entered == ["first"]

    release_first.set()
    await asyncio.gather(first, second)
    assert entered == ["first", "second"]


@pytest.mark.asyncio
async def test_different_sessions_evaluate_concurrently(tmp_path) -> None:
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def evaluate(
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
    ) -> HookResponse:
        del event, variables, eval_context
        entered.add(session_id)
        if entered == {"platform-a", "platform-b"}:
            both_entered.set()
        await release.wait()
        return HookResponse(decision="allow")

    handler = _handler_with_fake_engine(evaluate)
    task_a = asyncio.create_task(
        handler._evaluate_rules(
            _event(
                HookEventType.BEFORE_TOOL,
                platform_session_id="platform-a",
                data={"name": "a"},
                cwd=str(tmp_path),
            )
        )
    )
    task_b = asyncio.create_task(
        handler._evaluate_rules(
            _event(
                HookEventType.BEFORE_TOOL,
                platform_session_id="platform-b",
                data={"name": "b"},
                cwd=str(tmp_path),
            )
        )
    )

    await asyncio.wait_for(both_entered.wait(), timeout=1)
    release.set()
    await asyncio.gather(task_a, task_b)


@pytest.mark.asyncio
async def test_session_end_cleanup_waits_for_queued_same_session_event(tmp_path) -> None:
    end_entered = asyncio.Event()
    release_end = asyncio.Event()
    queued_entered = asyncio.Event()
    release_queued = asyncio.Event()

    async def evaluate(
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
    ) -> HookResponse:
        del session_id, variables, eval_context
        if event.event_type == HookEventType.SESSION_END:
            end_entered.set()
            await release_end.wait()
        else:
            queued_entered.set()
            await release_queued.wait()
        return HookResponse(decision="allow")

    handler = _handler_with_fake_engine(evaluate)
    session_end = asyncio.create_task(
        handler._evaluate_rules(
            _event(HookEventType.SESSION_END, data={"name": "end"}, cwd=str(tmp_path))
        )
    )
    await asyncio.wait_for(end_entered.wait(), timeout=1)

    queued = asyncio.create_task(
        handler._evaluate_rules(
            _event(HookEventType.BEFORE_TOOL, data={"name": "queued"}, cwd=str(tmp_path))
        )
    )
    await asyncio.sleep(0.05)

    assert not queued_entered.is_set()
    assert "platform-session" in handler._eval_locks

    release_end.set()
    await asyncio.wait_for(queued_entered.wait(), timeout=1)
    assert "platform-session" in handler._eval_locks

    release_queued.set()
    await asyncio.gather(session_end, queued)
    assert "platform-session" not in handler._eval_locks


@pytest.mark.asyncio
async def test_loaded_skill_observer_persists_before_next_same_session_event(
    db: LocalDatabase,
    tmp_path,
) -> None:
    platform_session_id = "platform-skill-session"
    SessionVariableManager(db).merge_variables(
        platform_session_id,
        {"baseline_dirty_files": [], "session_edited_files": []},
    )

    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="block-until-code-index-loaded",
        definition_json=RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            when="not skill_loaded('code-index')",
            effects=[
                RuleEffect(
                    type="block",
                    reason="code-index must be loaded",
                )
            ],
        ).model_dump_json(),
        workflow_type="rule",
        priority=10,
        enabled=True,
        sources=None,
    )

    engine = RuleEngine(db)
    original_evaluate = engine.evaluate
    skill_eval_entered = asyncio.Event()
    release_skill_eval = asyncio.Event()

    async def delayed_evaluate(
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
    ) -> HookResponse:
        if event.event_type == HookEventType.AFTER_TOOL:
            assert variables["loaded_skills"] == ["code-index"]
            skill_eval_entered.set()
            await release_skill_eval.wait()
        return await original_evaluate(
            event=event,
            session_id=session_id,
            variables=variables,
            eval_context=eval_context,
        )

    engine.evaluate = delayed_evaluate  # type: ignore[method-assign]
    handler = WorkflowHookHandler(rule_engine=engine)

    skill_event = _event(
        HookEventType.AFTER_TOOL,
        platform_session_id=platform_session_id,
        cwd=str(tmp_path),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-skills",
                "tool_name": "get_skill",
                "arguments": {"name": "code-index"},
            },
            "mcp_server": "gobby-skills",
            "mcp_tool": "get_skill",
            "tool_output": {
                "success": True,
                "result": {"skill": {"name": "code-index"}},
            },
        },
    )
    gated_tool_event = _event(
        HookEventType.BEFORE_TOOL,
        platform_session_id=platform_session_id,
        cwd=str(tmp_path),
        data={
            "tool_name": "Bash",
            "tool_input": {"command": "rg _evaluate_rules src/gobby/workflows/hooks.py"},
        },
    )

    skill_task = asyncio.create_task(handler._evaluate_rules(skill_event))
    await asyncio.wait_for(skill_eval_entered.wait(), timeout=1)

    gated_task = asyncio.create_task(handler._evaluate_rules(gated_tool_event))
    await asyncio.sleep(0.05)
    assert not gated_task.done()

    release_skill_eval.set()
    skill_response, gated_response = await asyncio.gather(skill_task, gated_task)

    assert skill_response.decision == "allow"
    assert gated_response.decision == "allow"
    variables = SessionVariableManager(db).get_variables(platform_session_id)
    assert variables["loaded_skills"] == ["code-index"]
