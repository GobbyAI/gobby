"""Database deadlines must survive hook fallbacks and remain retryable."""

import logging
from collections.abc import Iterator
from copy import deepcopy
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
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.async_ops import IndeterminateCommitError
from gobby.storage.hub.operation_deadline import DatabaseOperationDeadlineExceeded
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime, WorkflowEvaluationTimeout
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observer_context_usage import detect_context_compact_guidance
from gobby.workflows.observer_plan_mode import resolve_plan_mode
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.templates import TemplateEngine


@pytest.fixture
def real_deadline_handler(
    temp_db: HubDatabase, tmp_path: Path, mocker: MockerFixture
) -> Iterator[tuple[WorkflowHookHandler, MagicMock, MagicMock]]:
    tasks = MagicMock()
    tasks.get_task.return_value = None
    variables = MagicMock()
    variables.get_variables.return_value = {
        "baseline_dirty_files": [],
        "session_edited_files": [],
        "_variable_defaults_loaded": True,
        "project": {"path": str(tmp_path)},
        "context_compact_guidance_kind": "warn",
        "context_compact_guidance_message": "Existing handoff guidance",
    }
    handler = WorkflowHookHandler(
        timeout=5,
        rule_engine=RuleEngine(temp_db, task_manager=tasks),
        evaluation_runtime=WorkflowEvaluationRuntime(max_workers=1),
    )
    handler._session_var_manager = variables
    mocker.patch.object(handler, "_resolve_project_path", return_value=str(tmp_path))
    try:
        yield handler, variables, tasks
    finally:
        handler.shutdown()


def _deadline_event(event_type: HookEventType, cwd: Path | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="external-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": "11111111-1111-4111-8111-111111111111"},
        cwd=str(cwd) if cwd is not None else None,
    )


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
@pytest.mark.parametrize("event_type", [HookEventType.BEFORE_AGENT, HookEventType.BEFORE_TOOL])
def test_real_rule_condition_deadline_retries_without_committing_effects(
    temp_db: HubDatabase,
    tmp_path: Path,
    real_deadline_handler: tuple[WorkflowHookHandler, MagicMock, MagicMock],
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    event_type: HookEventType,
) -> None:
    handler, variables, tasks = real_deadline_handler
    error = error_type("task condition query expired")
    tasks.get_task.side_effect = [error, None]
    RuleDefinitionManager(temp_db).create(
        name="deadline-retry-directive",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent(event_type.value),
            when="not task_state_in('task-under-review', 'closed')",
            effects=[
                RuleEffect(type="inject_context", template="Load the plan skill."),
                RuleEffect(type="set_variable", variable="directive_evaluated", value=True),
            ],
        ).model_dump_json(),
        enabled=True,
    )
    event = _deadline_event(event_type, tmp_path)
    stored = deepcopy(variables.get_variables.return_value)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(WorkflowEvaluationTimeout) as raised:
            handler.evaluate(event)
        assert raised.value.__cause__ is error
        variables.merge_variables.assert_not_called()
        assert variables.get_variables.return_value == stored

        response = handler.evaluate(event)

    assert "Load the plan skill." in (response.context or "")
    assert variables.merge_variables.call_args.args[1]["directive_evaluated"] is True
    assert tasks.get_task.call_count == 2
    assert not caplog.records


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
@pytest.mark.parametrize("event_type", [HookEventType.BEFORE_AGENT, HookEventType.BEFORE_TOOL])
@pytest.mark.parametrize(
    "value",
    ['{{ task_state_in("#1", "closed") }}', 'task_state_in("#1", "closed") and True'],
    ids=["jinja", "expression"],
)
def test_real_set_variable_deadline_retries_without_saving_raw_or_partial_values(
    temp_db: HubDatabase,
    tmp_path: Path,
    real_deadline_handler: tuple[WorkflowHookHandler, MagicMock, MagicMock],
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    event_type: HookEventType,
    value: str,
) -> None:
    handler, variables, tasks = real_deadline_handler
    error = error_type("task value query expired")
    tasks.get_task.side_effect = [error, None]
    variables.get_variables.return_value["task_is_closed"] = "previous value"
    RuleDefinitionManager(temp_db).create(
        name="deadline-retry-variable",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent(event_type.value),
            effects=[
                RuleEffect(type="set_variable", variable="earlier_value", value=1),
                RuleEffect(type="set_variable", variable="task_is_closed", value=value),
                RuleEffect(type="set_variable", variable="following_value", value=2),
            ],
        ).model_dump_json(),
        enabled=True,
    )
    event = _deadline_event(event_type, tmp_path)
    stored = deepcopy(variables.get_variables.return_value)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(WorkflowEvaluationTimeout) as raised:
            handler.evaluate(event)
        assert raised.value.__cause__ is error
        variables.merge_variables.assert_not_called()
        assert variables.get_variables.return_value == stored

        assert handler.evaluate(event).decision == "allow"

    variables.merge_variables.assert_called_once()
    updates = variables.merge_variables.call_args.args[1]
    assert updates["task_is_closed"] is False
    assert updates["earlier_value"] == 1
    assert updates["following_value"] == 2
    assert tasks.get_task.call_count == 2
    assert not caplog.records


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
@pytest.mark.parametrize("render_kind", ["inline", "file"])
def test_template_engine_deadline_retries_without_logging_generic_errors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    render_kind: str,
) -> None:
    source = '{{ task_state_in("#1", "closed") }}'
    (tmp_path / "value.j2").write_text(source)
    tasks = MagicMock()
    error = error_type("template query expired")
    tasks.get_task.side_effect = [error, None]
    context = build_condition_helpers(task_manager=tasks)
    engine = TemplateEngine(template_dirs=[str(tmp_path)])
    render = engine.render if render_kind == "inline" else engine.render_file
    template = source if render_kind == "inline" else "value.j2"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(error_type) as raised:
            render(template, context)
        assert raised.value is error
        assert render(template, context) == "False"

    assert tasks.get_task.call_count == 2
    assert not caplog.records


@pytest.mark.parametrize(
    "value,wrote,expected",
    [
        ("{{ missing_helper() }}", True, "{{ missing_helper() }}"),
        ("variables.get('missing') + 1", False, "previous value"),
    ],
    ids=["invalid-jinja", "invalid-expression"],
)
def test_set_variable_keeps_ordinary_invalid_value_fallbacks(
    temp_db: HubDatabase, value: str, wrote: bool, expected: str
) -> None:
    variables: dict[str, Any] = {"target": "previous value"}
    effect = RuleEffect(type="set_variable", variable="target", value=value)

    applied = RuleEngine(temp_db)._apply_set_variable(effect, variables, {"variables": variables})

    assert applied is wrote
    assert variables == {"target": expected}


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
def test_real_context_observer_deadline_retries_without_clearing_stored_guidance(
    tmp_path: Path,
    real_deadline_handler: tuple[WorkflowHookHandler, MagicMock, MagicMock],
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    handler, variables, _tasks = real_deadline_handler
    error = error_type("context usage query expired")
    sessions = MagicMock()
    sessions.get.side_effect = [
        error,
        SimpleNamespace(context_used_tokens=260_000, context_window=1_000_000),
    ]
    handler._session_manager = sessions
    # Keep the failure at the real context-usage observer's session lookup.
    mocker.patch("gobby.workflows.observer_plan_mode.resolve_plan_mode")
    event = _deadline_event(HookEventType.BEFORE_AGENT, tmp_path)
    stored = deepcopy(variables.get_variables.return_value)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(WorkflowEvaluationTimeout) as raised:
            handler.evaluate(event)
        assert raised.value.__cause__ is error
        variables.merge_variables.assert_not_called()
        assert variables.get_variables.return_value == stored

        assert handler.evaluate(event).decision == "allow"

    updates = variables.merge_variables.call_args.args[1]
    assert "260k tokens" in updates["context_compact_guidance_message"]
    assert sessions.get.call_count == 2
    assert not caplog.records


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
def test_context_observer_preserves_guidance_and_counters_until_session_read_succeeds(
    error_type: type[Exception],
) -> None:
    variables: dict[str, Any] = {
        "context_compact_guidance_kind": "warn",
        "context_compact_guidance_message": "Existing handoff guidance",
        "turns_since_compact": 3,
        "_context_usage_turn_seq": 3,
    }
    stored = dict(variables)
    sessions = MagicMock()
    error = error_type("context usage query expired")
    sessions.get.side_effect = [error, None]

    with pytest.raises(error_type) as raised:
        detect_context_compact_guidance(variables, "session-id", sessions)
    assert raised.value is error
    assert variables == stored

    detect_context_compact_guidance(variables, "session-id", sessions)
    assert variables["context_compact_guidance_kind"] == ""
    assert variables["context_compact_guidance_message"] == ""
    assert variables["turns_since_compact"] == 4
    assert variables["_context_usage_turn_seq"] == 4


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
@pytest.mark.parametrize("entrypoint", ["evaluate", "evaluate_value"])
def test_expression_entrypoints_preserve_task_query_deadline(
    error_type: type[Exception], entrypoint: str
) -> None:
    error = error_type("task condition query expired")
    tasks = MagicMock()
    tasks.get_task.side_effect = error
    evaluator = SafeExpressionEvaluator({}, build_condition_helpers(task_manager=tasks))

    with pytest.raises(error_type) as raised:
        getattr(evaluator, entrypoint)("task_state_in('task-under-review', 'closed')")

    assert raised.value is error


@pytest.mark.parametrize("error_type", [DatabaseOperationDeadlineExceeded, QueryCanceled])
def test_template_project_lookup_preserves_database_deadline(
    temp_db: HubDatabase,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    error = error_type("project context query expired")
    mocker.patch("gobby.storage.sessions.SessionManager.get", side_effect=error)
    event = _deadline_event(HookEventType.BEFORE_AGENT)

    with caplog.at_level(logging.WARNING), pytest.raises(error_type) as raised:
        RuleEngine(temp_db)._resolve_project_info(event)

    assert raised.value is error
    assert not caplog.records


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
