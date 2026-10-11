"""Focused tests for hook helper modules extracted from HookManager."""

from __future__ import annotations

import json
from asyncio import AbstractEventLoop
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.broadcaster import schedule_hook_broadcast
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.project_context import ProjectIdResolver, resolve_hook_project_context
from gobby.hooks.rule_evaluator import WorkflowRuleEvaluator
from gobby.hooks.session_ref_resolution import resolve_session_refs_in_tool_input
from gobby.storage.projects import GLOBAL_PROJECT_ID, PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit


def _event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    event = HookEvent(
        event_type=event_type,
        session_id="external-1",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data or {},
        machine_id="21000000-0000-4000-8000-000000000001",
    )
    event.project_id = "proj-1"
    return event


class TestProjectIdResolver:
    def test_explicit_project_id_passthrough(self) -> None:
        resolver = ProjectIdResolver()
        assert resolver.resolve("proj-123", "/tmp/project") == "proj-123"

    def test_no_cwd_uses_personal_workspace(self) -> None:
        resolver = ProjectIdResolver()
        assert resolver.resolve(None, None) == PERSONAL_PROJECT_ID

    def test_project_context_is_ensured(self) -> None:
        ensure_project = MagicMock()
        resolver = ProjectIdResolver(ensure_project_in_db=ensure_project)

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "proj-abc", "name": "demo"},
        ):
            assert resolver.resolve(None, "/tmp/project") == "proj-abc"

        ensure_project.assert_called_once_with({"id": "proj-abc", "name": "demo"})

    def test_missing_project_context_raises(self) -> None:
        resolver = ProjectIdResolver()

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with pytest.raises(ValueError, match="gobby init"):
                resolver.resolve(None, "/tmp/project")


class TestHookProjectContext:
    def test_sessionless_hook_cwd_overrides_inherited_project_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text(
            json.dumps({"id": "cwd-project", "name": "CWD Project"})
        )
        monkeypatch.setenv("GOBBY_PROJECT_ID", "inherited-project")
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="missing-session",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={},
            cwd=str(tmp_path),
        )
        resolver = ProjectIdResolver()

        resolution = resolve_hook_project_context(
            event,
            session_manager=None,
            resolve_project_id=resolver.resolve,
        )

        assert resolution.project_id == "cwd-project"
        assert resolution.source == "cwd"
        assert event.project_id == "cwd-project"

    def test_contract_probe_cwd_uses_global_project(self) -> None:
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="probe-session",
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={"cwd": "/private/tmp/gobby-contract-probe-15038"},
            machine_id="21000000-0000-4000-8000-000000000001",
        )
        resolve_project_id = MagicMock(side_effect=AssertionError("should not resolve cwd"))

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            resolution = resolve_hook_project_context(
                event,
                session_manager=None,
                resolve_project_id=resolve_project_id,
            )

        assert resolution.project_id == GLOBAL_PROJECT_ID
        assert resolution.source == "contract-probe"
        assert resolution.skipped is False
        assert event.project_id == GLOBAL_PROJECT_ID
        assert event.data["project_id"] == GLOBAL_PROJECT_ID
        resolve_project_id.assert_not_called()

    def test_contract_probe_cwd_overrides_daemon_current_context(self) -> None:
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="probe-session",
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={"cwd": "/tmp/gobby-contract-probe-15038"},
            machine_id="21000000-0000-4000-8000-000000000001",
        )

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "daemon-project", "name": "daemon"},
        ):
            resolution = resolve_hook_project_context(
                event,
                session_manager=None,
                resolve_project_id=MagicMock(),
            )

        assert resolution.project_id == GLOBAL_PROJECT_ID
        assert resolution.source == "contract-probe"

    def test_non_probe_tmp_cwd_still_uses_normal_project_resolution(self) -> None:
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="tmp-session",
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={"cwd": "/private/tmp/not-a-probe"},
            machine_id="21000000-0000-4000-8000-000000000001",
        )
        resolve_project_id = MagicMock(return_value="resolved-project")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            resolution = resolve_hook_project_context(
                event,
                session_manager=None,
                resolve_project_id=resolve_project_id,
            )

        assert resolution.project_id == "resolved-project"
        assert resolution.source == "cwd"
        resolve_project_id.assert_called_once_with(None, "/private/tmp/not-a-probe")


class TestSessionRefResolution:
    def test_resolves_call_tool_top_level_and_nested_session_ids(self) -> None:
        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = [
            "wrapper-session",
            "nested-session",
        ]
        event = _event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "session_id": "#3",
                    "arguments": {"session_id": "#4"},
                },
            }
        )

        resolve_session_refs_in_tool_input(event, session_manager)

        assert event.data["tool_input"]["session_id"] == "wrapper-session"
        assert event.data["tool_input"]["arguments"]["session_id"] == "nested-session"
        assert session_manager.resolve_session_reference.call_args_list[0].args == ("#3", "proj-1")
        assert session_manager.resolve_session_reference.call_args_list[1].args == ("#4", "proj-1")

    def test_variable_tools_preserve_session_refs(self) -> None:
        session_manager = MagicMock()
        event = _event(
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {"name": "flag", "value": True, "session_id": "#3"},
            }
        )

        resolve_session_refs_in_tool_input(event, session_manager)

        assert event.data["tool_input"]["session_id"] == "#3"
        session_manager.resolve_session_reference.assert_not_called()


class TestWorkflowRuleEvaluator:
    @pytest.mark.parametrize(
        "error", [IndexError("matcher index out of range"), RuntimeError("matcher crashed")]
    )
    def test_before_tool_evaluation_exception_blocks_with_diagnostic(
        self, error: Exception
    ) -> None:
        workflow_handler = MagicMock()
        workflow_handler.handle.side_effect = error
        dispatch = MagicMock()
        evaluator = WorkflowRuleEvaluator(
            workflow_handler=workflow_handler,
            dispatch_mcp_calls=dispatch,
            format_discovery_result=MagicMock(),
            database=MagicMock(),
            logger=MagicMock(),
        )

        with patch("gobby.hooks.rule_evaluator.audit_source_block_sync") as audit:
            context, blocking = evaluator.evaluate(_event())

        assert context is None
        assert blocking is not None
        assert blocking.decision == "block"
        assert blocking.reason is not None
        assert type(error).__name__ in blocking.reason
        assert str(error) in blocking.reason
        dispatch.assert_not_called()
        audit.assert_called_once()
        assert audit.call_args.kwargs["rule_id"] == "hook-before-tool-safety"

    @pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.STOP_FAILURE])
    def test_stop_evaluation_exception_keeps_safety_block(self, event_type: HookEventType) -> None:
        workflow_handler = MagicMock()
        workflow_handler.handle.side_effect = RuntimeError("matcher crashed")
        evaluator = WorkflowRuleEvaluator(
            workflow_handler=workflow_handler,
            dispatch_mcp_calls=MagicMock(),
            format_discovery_result=MagicMock(),
            database=MagicMock(),
            logger=MagicMock(),
        )

        with patch("gobby.hooks.rule_evaluator.audit_source_block_sync") as audit:
            context, blocking = evaluator.evaluate(_event(event_type))

        assert context is None
        assert blocking == HookResponse(
            decision="block", reason="Workflow evaluation failed; blocking stop for safety."
        )
        assert audit.call_args.kwargs["rule_id"] == "hook-stop-safety"

    @pytest.mark.parametrize(
        "event_type",
        [
            event
            for event in HookEventType
            if event
            not in {HookEventType.BEFORE_TOOL, HookEventType.STOP, HookEventType.STOP_FAILURE}
        ],
    )
    def test_other_evaluation_exceptions_keep_fail_open(self, event_type: HookEventType) -> None:
        workflow_handler = MagicMock()
        error = RuntimeError("matcher crashed")
        workflow_handler.handle.side_effect = error
        logger = MagicMock()
        evaluator = WorkflowRuleEvaluator(
            workflow_handler=workflow_handler,
            dispatch_mcp_calls=MagicMock(),
            format_discovery_result=MagicMock(),
            database=MagicMock(),
            logger=logger,
        )

        with patch("gobby.hooks.rule_evaluator.audit_source_block_sync") as audit:
            context, blocking = evaluator.evaluate(_event(event_type))

        assert context is None
        assert blocking is None
        workflow_handler.handle.assert_called_once()
        assert logger.exception.call_args.args == (
            "Workflow evaluation failed: %s",
            error,
        )
        audit.assert_not_called()

    def test_allow_merges_workflow_and_discovery_context(self) -> None:
        workflow_handler = MagicMock()
        workflow_response = HookResponse(
            decision="allow",
            metadata={"mcp_calls": [{"server": "_proxy", "tool": "list_tools"}]},
        )
        workflow_response.add_context(("rule:base", "base context"))
        workflow_handler.handle.return_value = workflow_response
        dispatch_mcp_calls = MagicMock(
            return_value=[
                {
                    "server": "_proxy",
                    "tool": "list_tools",
                    "inject_result": True,
                    "success": True,
                    "result": {"tools": []},
                }
            ]
        )
        evaluator = WorkflowRuleEvaluator(
            workflow_handler=workflow_handler,
            dispatch_mcp_calls=dispatch_mcp_calls,
            format_discovery_result=MagicMock(return_value="discovery context"),
            database=MagicMock(),
            logger=MagicMock(),
        )

        context, blocking = evaluator.evaluate(_event())

        assert context == [
            ("rule:base", "base context"),
            ("mcp:_proxy/list_tools", "discovery context"),
        ]
        assert blocking is None
        dispatch_mcp_calls.assert_called_once()

    def test_block_on_failure_overrides_workflow_allow(self) -> None:
        workflow_handler = MagicMock()
        workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            metadata={"mcp_calls": [{"server": "_proxy", "tool": "list_tools"}]},
        )
        evaluator = WorkflowRuleEvaluator(
            workflow_handler=workflow_handler,
            dispatch_mcp_calls=MagicMock(
                return_value=[
                    {
                        "server": "_proxy",
                        "tool": "list_tools",
                        "inject_result": False,
                        "block_on_failure": True,
                        "success": False,
                        "result": {"error": "server missing"},
                    }
                ]
            ),
            format_discovery_result=MagicMock(),
            database=MagicMock(),
            logger=MagicMock(),
        )

        with patch("gobby.hooks.rule_evaluator.audit_source_block_sync") as audit:
            context, blocking = evaluator.evaluate(_event())

        assert context is None
        assert blocking == HookResponse(
            decision="block",
            reason="Auto-heal prerequisite failed: _proxy/list_tools: server missing",
        )
        audit.assert_called_once()


class TestBroadcastScheduling:
    @patch("gobby.hooks.broadcaster.asyncio.get_running_loop", side_effect=RuntimeError)
    @patch("gobby.hooks.broadcaster.asyncio.run_coroutine_threadsafe")
    def test_schedules_on_captured_loop(
        self, mock_threadsafe: MagicMock, mock_get_loop: MagicMock
    ) -> None:
        broadcaster = MagicMock()

        async def broadcast_event(*args: Any, **kwargs: Any) -> None:
            return None

        def close_coro(coro: Coroutine[Any, Any, None], loop: AbstractEventLoop) -> MagicMock:
            coro.close()
            return MagicMock()

        mock_threadsafe.side_effect = close_coro
        broadcaster.broadcast_event = MagicMock(side_effect=broadcast_event)
        loop = MagicMock()
        event = _event()
        response = HookResponse(decision="allow")

        schedule_hook_broadcast(
            broadcaster,
            event,
            response,
            loop,
            MagicMock(),
        )

        broadcaster.broadcast_event.assert_called_once()
        assert broadcaster.broadcast_event.call_args.args[0] is event
        assert broadcaster.broadcast_event.call_args.args[1] is response
        assert loop is mock_threadsafe.call_args.args[1]
        mock_get_loop.assert_called_once()
        mock_threadsafe.assert_called_once()
