"""Database deadlines must survive hook fallbacks and remain retryable."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from psycopg.errors import QueryCanceled
from pytest_mock import MockerFixture

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.rule_evaluator import WorkflowRuleEvaluator
from gobby.storage.hub.async_ops import IndeterminateCommitError
from gobby.storage.hub.operation_deadline import DatabaseOperationDeadlineExceeded
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime, WorkflowEvaluationTimeout
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observer_plan_mode import resolve_plan_mode


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
@pytest.mark.parametrize("event_type", [HookEventType.BEFORE_AGENT, HookEventType.BEFORE_TOOL])
@pytest.mark.parametrize("stage", ["variables", "defaults", "step", "observer", "rules"])
def test_database_timeout_reaches_evaluator_and_next_delivery_can_retry(
    tmp_path: Path,
    mocker: MockerFixture,
    error_type: type[Exception],
    stage: str,
    event_type: HookEventType,
) -> None:
    error = error_type("statement deadline exceeded")
    response = HookResponse(decision="allow", context="load the plan skill")
    rule_engine = MagicMock()
    rule_engine.evaluate = AsyncMock(return_value=response)
    session_vars = MagicMock()
    session_vars.get_variables.return_value = {
        "baseline_dirty_files": [],
        "session_edited_files": [],
        "is_spawned_agent": stage == "step",
    }
    if stage != "defaults":
        session_vars.get_variables.return_value["_variable_defaults_loaded"] = True
    workflow_log = mocker.patch("gobby.workflows.hooks.logger")
    runtime = WorkflowEvaluationRuntime(max_workers=1)
    handler = WorkflowHookHandler(timeout=5, evaluation_runtime=runtime)
    handler.rule_engine = rule_engine
    handler._session_var_manager = session_vars
    mocker.patch.object(handler, "_resolve_project_path", return_value=str(tmp_path))
    if stage != "observer":
        mocker.patch.object(handler, "_run_observers", return_value=set())
    if stage == "variables":
        session_vars.get_variables.side_effect = [error, session_vars.get_variables.return_value]
    elif stage == "defaults":
        mocker.patch(
            "gobby.workflows.variable_defaults.merge_unloaded_variable_defaults",
            side_effect=[error, {}],
        )
    elif stage == "step":
        mocker.patch(
            "gobby.workflows.hooks.get_active_step_workflow_context", side_effect=[error, None]
        )
    elif stage == "observer":
        observer = (
            "resolve_plan_mode"
            if event_type == HookEventType.BEFORE_AGENT
            else "reconcile_native_mode"
        )
        mocker.patch(f"gobby.workflows.observer_plan_mode.{observer}", side_effect=[error, None])
    else:
        rule_engine.evaluate.side_effect = [error, response]

    event = HookEvent(
        event_type=event_type,
        session_id="external-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": "platform-session"},
        cwd=str(tmp_path),
    )
    evaluator_log = MagicMock()
    dispatch = MagicMock(return_value=[])
    evaluator = WorkflowRuleEvaluator(
        workflow_handler=handler,
        dispatch_mcp_calls=dispatch,
        format_discovery_result=MagicMock(return_value=""),
        database=MagicMock(),
        logger=evaluator_log,
    )
    try:
        with pytest.raises(WorkflowEvaluationTimeout) as raised:
            evaluator.evaluate(event)
        assert raised.value.__cause__ is error
        assert raised.value.event_type == event_type.value
        assert raised.value.session_id == "platform-session"
        dispatch.assert_not_called()
        session_vars.merge_variables.assert_not_called()

        assert handler.evaluate(event).context == "load the plan skill"
        workflow_log.warning.assert_not_called()
        workflow_log.exception.assert_not_called()
        evaluator_log.error.assert_not_called()
    finally:
        handler.shutdown()


@pytest.mark.asyncio
async def test_indeterminate_commit_is_not_reclassified_as_retryable_timeout(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    error = IndeterminateCommitError("commit outcome is unknown")
    handler = WorkflowHookHandler(timeout=5)
    mocker.patch.object(handler, "_evaluate_rules", side_effect=error)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="external-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        cwd=str(tmp_path),
    )

    with pytest.raises(IndeterminateCommitError) as raised:
        await handler.evaluate_async(event)
    assert raised.value is error


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
def test_plan_mode_session_lookup_does_not_swallow_deadlines(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    error = error_type("session lookup deadline")
    sessions = MagicMock()
    sessions.get.side_effect = [error, SimpleNamespace(session_type="web_chat", chat_mode="plan")]
    variables: dict[str, Any] = {"plan_mode": False}
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="external-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"session_type": "web_chat"},
        cwd=str(tmp_path),
    )

    with pytest.raises(error_type) as raised:
        resolve_plan_mode(event, variables, "platform-session", sessions)
    assert raised.value is error
    assert variables == {"plan_mode": False}

    resolve_plan_mode(event, variables, "platform-session", sessions)
    assert variables["plan_mode"] is True
    assert sessions.get.call_count == 2
