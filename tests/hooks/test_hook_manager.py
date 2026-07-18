"""Tests for HookManager edge cases and error handling."""

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.dispatchers.mcp import (
    PROJECT_MEMORY_CLOSE_TAG,
    PROJECT_MEMORY_CONTEXT_BUDGET,
    PROJECT_MEMORY_OPEN_TAG,
    _format_project_memories,
    _project_memory_next_line_budget,
    _project_memory_render_len,
    _render_project_memory,
    run_coro_blocking,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.memory.context import format_memory_metadata_suffix
from gobby.storage.machines import LocalMachineManager
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


# ─── Fixtures ───────────────────────────────────────────────────────────
# mock_components and manager_with_mocks come from tests/hooks/conftest.py.


@pytest.fixture
def make_event() -> Callable[..., HookEvent]:
    """Factory for creating test HookEvents."""

    def _make(
        event_type: HookEventType = HookEventType.BEFORE_AGENT,
        source: SessionSource = SessionSource.CLAUDE,
        data: dict | None = None,
    ) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id="test-external-id",
            source=source,
            timestamp=datetime.now(UTC),
            data=data or {},
            machine_id="test-machine",
        )

    return _make


# ─── Tests for handle() method ──────────────────────────────────────────


def test_record_machine_ingress_upserts_payload_metadata(
    manager_with_mocks: HookManager,
    make_event: Callable[..., HookEvent],
    temp_db,
) -> None:
    manager_with_mocks._database = temp_db
    manager_with_mocks._session_manager = SessionManager(temp_db)
    event = make_event(
        data={
            "hostname": "workstation",
            "os": "Darwin",
            "machine_label": "desk",
            "tailscale_name": "workstation.tailnet",
        }
    )
    event.machine_id = "machine-hook"

    manager_with_mocks._record_machine_ingress(event)

    machine = LocalMachineManager(temp_db).get("machine-hook")
    assert machine is not None
    assert machine.hostname == "workstation"
    assert machine.os == "Darwin"
    assert machine.label == "desk"
    assert machine.tailscale_name == "workstation.tailnet"

    payload_event = make_event(
        data={
            "machineId": "payload-machine",
            "hostname": "laptop",
            "os": "Linux",
            "machine_label": "travel",
        }
    )
    payload_event.machine_id = "unknown-machine"

    manager_with_mocks._record_machine_ingress(payload_event)

    payload_machine = LocalMachineManager(temp_db).get("payload-machine")
    assert payload_machine is not None
    assert payload_machine.hostname == "laptop"
    assert payload_machine.os == "Linux"
    assert payload_machine.label == "travel"


class TestHandleInternalDaemonNotReady:
    """Tests for _handle_internal when daemon is not ready."""

    @pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.AFTER_AGENT])
    def test_terminal_hook_blocks_before_rule_evaluation(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable[..., HookEvent],
        event_type: HookEventType,
    ) -> None:
        manager = manager_with_mocks
        manager._health_monitor.get_cached_status.return_value = (
            False,
            None,
            "unreachable",
            "Connection refused",
        )
        manager._health_monitor.check_now.return_value = False
        manager._workflow_handler.handle.reset_mock()

        with patch("time.sleep"), patch.object(manager, "_handle_after_daemon_ready") as downstream:
            response = manager._handle_internal(make_event(event_type=event_type))

        assert response.decision == "block"
        downstream.assert_not_called()
        manager._workflow_handler.handle.assert_not_called()

    def test_handle_returns_allow_when_daemon_not_ready_for_non_critical(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Non-critical hooks fail-open when daemon is not ready after retries."""
        manager = manager_with_mocks
        manager._health_monitor.get_cached_status.return_value = (
            False,
            "unavailable",
            "unreachable",
            "Connection refused",
        )
        manager._health_monitor.check_now.return_value = False

        event = make_event(event_type=HookEventType.BEFORE_AGENT)
        response = manager._handle_internal(event)

        assert response.decision == "allow"
        assert "unreachable" in (response.reason or "")

    @pytest.mark.parametrize(
        "event_type",
        [HookEventType.SESSION_START, HookEventType.AFTER_AGENT],
    )
    def test_handle_retries_for_critical_hooks(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
        event_type: HookEventType,
    ) -> None:
        """Critical hooks retry daemon health checks before failing open."""
        manager = manager_with_mocks
        manager._health_monitor.get_cached_status.return_value = (
            False,
            None,
            "starting",
            "not ready",
        )
        # check_now returns True on second call (recovery)
        manager._health_monitor.check_now.side_effect = [False, True]

        event = make_event(event_type=event_type)
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler

        # Mock rule evaluation to allow
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()
        manager._session_lookup.resolve.return_value = None

        with patch("time.sleep"):
            response = manager._handle_internal(event)

        assert response.decision == "allow"
        handler.assert_called_once()


class TestHandleInternalEventHandlerError:
    """Tests for _handle_internal when event handler raises."""

    def test_handler_exception_returns_allow(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """When event handler raises, response should fail-open."""
        manager = manager_with_mocks
        handler = MagicMock(side_effect=RuntimeError("Handler crashed"))
        manager._event_handlers.get_handler.return_value = handler

        # Mock rule evaluation to allow
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._session_lookup.resolve.return_value = None

        event = make_event(event_type=HookEventType.AFTER_AGENT)
        response = manager._handle_internal(event)

        assert response.decision == "allow"
        assert "Handler error" in (response.reason or "")

    def test_no_handler_for_event_type(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Unknown event types fail-open with allow."""
        manager = manager_with_mocks
        manager._event_handlers.get_handler.return_value = None
        manager._session_lookup.resolve.return_value = None

        event = make_event(event_type=HookEventType.NOTIFICATION)
        response = manager._handle_internal(event)

        assert response.decision == "allow"


class TestHandleSessionStart:
    """Tests for SESSION_START handler ordering (handler before rules)."""

    def test_session_start_runs_handler_before_rules(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """For SESSION_START, handler runs first, then rules."""
        manager = manager_with_mocks
        call_order: list[str] = []

        def mock_handler(event: HookEvent) -> HookResponse:
            call_order.append("handler")
            return HookResponse(decision="allow")

        def mock_workflow_handle(event: HookEvent) -> HookResponse:
            call_order.append("rules")
            return HookResponse(decision="allow")

        manager._event_handlers.get_handler.return_value = mock_handler
        manager._workflow_handler.handle = mock_workflow_handle
        manager._session_lookup.resolve.return_value = None
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.SESSION_START)
        manager._handle_internal(event)

        assert call_order == ["handler", "rules"]

    def test_session_start_skips_pre_handler_session_lookup(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """SESSION_START must not resolve or auto-register a session before the handler runs."""
        manager = manager_with_mocks
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()
        manager._resolve_project_id = MagicMock(return_value=PERSONAL_PROJECT_ID)

        event = make_event(event_type=HookEventType.SESSION_START, data={"cwd": "/tmp/project"})
        manager._handle_internal(event)

        manager._session_lookup.resolve.assert_not_called()
        assert event.project_id == PERSONAL_PROJECT_ID
        handler.assert_called_once()

    def test_session_start_root_cwd_uses_platform_session_project(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Codex GUI cwd=/ must use platform session context instead of filesystem lookup."""
        manager = manager_with_mocks
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()
        manager._resolve_project_id = MagicMock(return_value="wrong-project")
        session = MagicMock()
        session.project_id = "project-from-session"
        manager._session_manager.get.return_value = session

        event = make_event(
            event_type=HookEventType.SESSION_START,
            source=SessionSource.CODEX,
            data={"session_id": "codex-session", "cwd": "/"},
        )
        event.metadata["_platform_session_id"] = "platform-session"
        manager._handle_internal(event)

        manager._resolve_project_id.assert_not_called()
        assert event.project_id == "project-from-session"
        assert event.data["project_id"] == "project-from-session"
        handler.assert_called_once()

    def test_session_start_root_cwd_without_context_skips(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """No-context Codex GUI startup from cwd=/ is skipped without project lookup noise."""
        manager = manager_with_mocks
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler
        manager._resolve_project_id = MagicMock(return_value="wrong-project")
        manager._enricher.enrich = MagicMock()

        with patch(
            "gobby.hooks.project_context._project_id_from_current_context", return_value=None
        ):
            event = make_event(
                event_type=HookEventType.SESSION_START,
                source=SessionSource.CODEX,
                data={"session_id": "codex-session", "cwd": "/"},
            )
            response = manager._handle_internal(event)

        assert response == HookResponse(decision="allow")
        assert event.project_id is None
        assert event.metadata == {}
        manager._resolve_project_id.assert_not_called()
        manager._enricher.enrich.assert_not_called()
        manager._event_handlers.get_handler.assert_not_called()
        handler.assert_not_called()


class TestHandleNonSessionStart:
    """Tests for non-SESSION_START handler ordering (rules before handler)."""

    def test_rules_and_webhooks_share_blocking_deadline(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        manager = manager_with_mocks
        manager._event_handlers.get_handler.return_value = MagicMock(
            return_value=HookResponse(decision="allow")
        )
        manager._session_lookup.resolve.return_value = None

        with (
            patch.object(
                manager,
                "_evaluate_workflow_rules",
                return_value=(None, None),
            ) as evaluate_rules,
            patch.object(
                manager,
                "_evaluate_blocking_webhooks",
                return_value=None,
            ) as evaluate_webhooks,
            patch("gobby.hooks.hook_manager.time.monotonic", return_value=100.0),
        ):
            manager._handle_internal(make_event(event_type=HookEventType.BEFORE_TOOL))

        rules_deadline = evaluate_rules.call_args.args[1]
        webhooks_deadline = evaluate_webhooks.call_args.args[1]
        assert rules_deadline == webhooks_deadline
        assert 100.0 < rules_deadline < 120.0

    def test_non_session_start_runs_rules_before_handler(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """For non-SESSION_START events, rules run first, then handler."""
        manager = manager_with_mocks
        call_order: list[str] = []

        def mock_handler(event: HookEvent) -> HookResponse:
            call_order.append("handler")
            return HookResponse(decision="allow")

        def mock_workflow_handle(event: HookEvent) -> HookResponse:
            call_order.append("rules")
            return HookResponse(decision="allow")

        manager._event_handlers.get_handler.return_value = mock_handler
        manager._workflow_handler.handle = mock_workflow_handle
        manager._session_lookup.resolve.return_value = None
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_AGENT)
        manager._handle_internal(event)

        assert call_order == ["rules", "handler"]


class TestHandleWorkflowBlock:
    """Tests for blocking responses from workflow rules."""

    def test_rules_block_prevents_handler(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """When rules block, handler is not called."""
        manager = manager_with_mocks
        handler = MagicMock()
        manager._event_handlers.get_handler.return_value = handler
        manager._session_lookup.resolve.return_value = None

        # Rules return block
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="block",
            reason="Blocked by rule",
        )

        event = make_event(event_type=HookEventType.BEFORE_TOOL)
        response = manager._handle_internal(event)

        assert response.decision == "block"
        handler.assert_not_called()


class TestHandlePostProcessing:
    """Tests for post-processing in _handle_internal."""

    def test_modified_input_propagated(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """_modified_input from metadata is propagated to response."""
        manager = manager_with_mocks
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler
        manager._session_lookup.resolve.return_value = None

        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            modified_input={"arg": "rewritten"},
            auto_approve=True,
        )

        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_TOOL)
        # Simulate stash
        event.metadata["_modified_input"] = {"arg": "rewritten"}
        event.metadata["_auto_approve"] = True

        response = manager._handle_internal(event)

        assert response.modified_input == {"arg": "rewritten"}
        assert response.auto_approve is True

    def test_input_coerced_flag(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """_input_coerced flag in data triggers auto-approve metadata."""
        manager = manager_with_mocks
        handler = MagicMock(return_value=HookResponse(decision="allow"))
        manager._event_handlers.get_handler.return_value = handler
        manager._session_lookup.resolve.return_value = None
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_TOOL)
        event.data["_input_coerced"] = True
        event.data["tool_input"] = {"key": "value"}

        manager._handle_internal(event)

        # The flag should have been consumed (popped)
        assert "_input_coerced" not in event.data

    def test_raw_tool_input_is_not_copied_to_response_metadata(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        manager = manager_with_mocks
        manager._event_handlers.get_handler.return_value = MagicMock(
            return_value=HookResponse(decision="allow")
        )
        manager._session_lookup.resolve.return_value = None
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()
        event = make_event(event_type=HookEventType.BEFORE_TOOL)
        event.metadata["raw_tool_input"] = {"secret": "unneeded-copy"}

        response = manager._handle_internal(event)

        assert "_raw_tool_input" not in response.metadata


class TestHookManagerHelpers:
    """Tests for HookManager helper methods."""

    def test_get_machine_id(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """get_machine_id returns a string."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-123"):
            result = manager_with_mocks.get_machine_id()

        assert result == "test-123"

    def test_get_machine_id_fallback(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """get_machine_id returns 'unknown-machine' when underlying returns None."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value=None):
            result = manager_with_mocks.get_machine_id()

        assert result == "unknown-machine"

    def test_resolve_project_id_with_explicit_id(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id returns explicit id when provided."""
        result = manager_with_mocks._resolve_project_id("proj-123", "/some/path")
        assert result == "proj-123"

    def test_resolve_project_id_from_cwd(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id resolves from cwd project context."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-abc", "name": "test"}
            with patch.object(manager_with_mocks, "_ensure_project_in_db"):
                result = manager_with_mocks._resolve_project_id(None, "/some/path")

        assert result == "proj-abc"

    def test_resolve_project_id_no_project_json_raises(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id raises when no project.json found."""
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with pytest.raises(ValueError, match="gobby init"):
                manager_with_mocks._resolve_project_id(None, "/some/path")

    def test_resolve_project_id_no_cwd_returns_personal(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_resolve_project_id returns personal workspace when no cwd."""
        result = manager_with_mocks._resolve_project_id(None, None)
        assert result == PERSONAL_PROJECT_ID


class TestFormatDiscoveryResult:
    """Tests for _format_discovery_result static method."""

    def test_format_list_mcp_servers(self) -> None:
        """Formats list_mcp_servers result correctly."""
        dr = {
            "tool": "list_mcp_servers",
            "result": {
                "servers": [
                    {"name": "gobby-tasks", "state": "connected"},
                    {"name": "gobby-memory", "state": "disconnected"},
                ]
            },
        }
        result = HookManager._format_discovery_result(dr)
        assert "gobby-tasks" in result
        assert "connected" in result
        assert "gobby-memory" in result

    def test_format_list_tools(self) -> None:
        """Formats list_tools result correctly."""
        dr = {
            "tool": "list_tools",
            "_args": {"server_name": "gobby-tasks"},
            "result": {
                "tools": [
                    {"name": "create_task", "brief": "Create a task"},
                ]
            },
        }
        result = HookManager._format_discovery_result(dr)
        assert "create_task" in result
        assert "gobby-tasks" in result

    def test_format_get_tool_schema(self) -> None:
        """Formats get_tool_schema result correctly."""
        dr = {
            "tool": "get_tool_schema",
            "result": {
                "tool": {
                    "name": "create_task",
                    "description": "Create a new task",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            },
        }
        result = HookManager._format_discovery_result(dr)
        assert "create_task" in result
        assert "Schema" in result

    def test_format_search_memories_adds_memory_id_with_metadata(self) -> None:
        """Formats injected memory search results with one memory ID suffix."""
        dr = {
            "tool": "search_memories",
            "result": {
                "memories": [
                    {
                        "id": "mm-abc123",
                        "content": "Use task-linked commits.",
                        "similarity": 0.92634,
                        "search_via": "keyword",
                    }
                ]
            },
        }

        result = HookManager._format_discovery_result(dr)

        assert "<project-memory>" in result
        assert (
            "- Use task-linked commits. (memory_id: mm-abc123, score: 0.9263, via: keyword)"
        ) in result
        assert result.count("memory_id: mm-abc123") == 1

    def test_format_project_memories_truncates_single_oversized_memory(self) -> None:
        """Truncates one oversized memory while preserving metadata and tags."""
        suffix = format_memory_metadata_suffix("mem-big", score=0.99, via="semantic")
        truncated_line = f"- {'A' * 20}...{suffix}"
        expected = "\n".join([PROJECT_MEMORY_OPEN_TAG, truncated_line, PROJECT_MEMORY_CLOSE_TAG])

        result = _format_project_memories(
            [
                {
                    "content": "A" * 200,
                    "id": "mem-big",
                    "similarity": 0.99,
                    "search_via": "semantic",
                }
            ],
            budget=len(expected),
        )

        assert result == expected

    def test_format_project_memories_returns_empty_when_body_cannot_fit(self) -> None:
        """Avoids wrapper-only project memory when no body line fits the budget."""
        result = _format_project_memories([{"content": "A"}], budget=1)

        assert result == ""

    def test_format_project_memories_includes_all_memories_within_budget(self) -> None:
        """Includes every valid memory when the rendered output fits."""
        first_suffix = format_memory_metadata_suffix("mem-1", score=0.91, via="semantic")
        second_suffix = format_memory_metadata_suffix("mem-2", score=0.82, via="keyword")
        expected = "\n".join(
            [
                PROJECT_MEMORY_OPEN_TAG,
                f"- Use task-linked commits.{first_suffix}",
                f"- Prefer focused validation.{second_suffix}",
                PROJECT_MEMORY_CLOSE_TAG,
            ]
        )

        result = _format_project_memories(
            [
                {
                    "content": "Use task-linked commits.",
                    "id": "mem-1",
                    "similarity": 0.91,
                    "search_via": "semantic",
                },
                {
                    "content": "Prefer focused validation.",
                    "id": "mem-2",
                    "similarity": 0.82,
                    "search_via": "keyword",
                },
            ],
            budget=len(expected),
        )

        assert result == expected

    def test_project_memory_length_helpers_match_actual_render(self) -> None:
        """Keeps helper budget math tied to the project-memory renderer."""
        cases = [
            ([], 0),
            (["- One memory."], 0),
            (["- One memory.", "- Two memory."], 2),
        ]
        budget = 200

        for body_lines, omitted_count in cases:
            assert _project_memory_render_len(body_lines, omitted_count) == len(
                _render_project_memory(body_lines, omitted_count)
            )
            assert _project_memory_next_line_budget(body_lines, omitted_count, budget) == (
                budget - len(_render_project_memory(body_lines + [""], omitted_count))
            )

    def test_format_project_memories_truncates_middle_memory_and_counts_omitted(
        self,
    ) -> None:
        """Truncates the first non-fitting memory and reports lower-ranked omissions."""
        first_suffix = format_memory_metadata_suffix("mem-1", score=0.91, via="semantic")
        second_suffix = format_memory_metadata_suffix("mem-2", score=0.82, via="keyword")
        expected = "\n".join(
            [
                PROJECT_MEMORY_OPEN_TAG,
                f"- First memory fits.{first_suffix}",
                f"- {'B' * 12}...{second_suffix}",
                "- ... 2 lower-ranked memories omitted due to context budget.",
                PROJECT_MEMORY_CLOSE_TAG,
            ]
        )

        result = _format_project_memories(
            [
                {
                    "content": "First memory fits.",
                    "id": "mem-1",
                    "similarity": 0.91,
                    "search_via": "semantic",
                },
                {
                    "content": "B" * 200,
                    "id": "mem-2",
                    "similarity": 0.82,
                    "search_via": "keyword",
                },
                {
                    "content": "Dropped third memory.",
                    "id": "mem-3",
                    "similarity": 0.7,
                    "search_via": "semantic",
                },
                {
                    "content": "Dropped fourth memory.",
                    "id": "mem-4",
                    "similarity": 0.6,
                    "search_via": "keyword",
                },
            ],
            budget=len(expected),
        )

        assert result == expected

    def test_format_search_memories_omits_review_lesson_memories(self) -> None:
        """Keeps raw review lessons out of generic project-memory injection."""
        dr = {
            "tool": "search_memories",
            "result": {
                "memories": [
                    {
                        "id": "review-raw",
                        "content": "# Review Lesson: Raw diagnostic should stay hidden",
                        "tags": ["review-lesson", "confirmed"],
                    },
                    {
                        "id": "mem-ok",
                        "content": "Use task-linked commits.",
                        "tags": ["test"],
                    },
                ]
            },
        }

        result = HookManager._format_discovery_result(dr)

        assert "# Review Lesson" not in result
        assert "review-raw" not in result
        assert "Use task-linked commits." in result
        assert "memory_id: mem-ok" in result

    def test_format_search_memories_bounds_output_before_adapter_truncation(self) -> None:
        """Keeps oversized project-memory output closed and traceable."""
        dr = {
            "tool": "search_memories",
            "result": {
                "memories": [
                    {
                        "id": "mem-big",
                        "content": "A" * (PROJECT_MEMORY_CONTEXT_BUDGET + 500),
                        "similarity": 0.99,
                    },
                    {
                        "id": "mem-dropped-1",
                        "content": "lower ranked one",
                        "similarity": 0.8,
                    },
                    {
                        "id": "mem-dropped-2",
                        "content": "lower ranked two",
                        "similarity": 0.7,
                    },
                ]
            },
        }

        result = HookManager._format_discovery_result(dr)

        assert len(result) <= PROJECT_MEMORY_CONTEXT_BUDGET
        assert result.startswith("<project-memory>")
        assert result.endswith("</project-memory>")
        assert "memory_id: mem-big" in result
        assert "2 lower-ranked memories omitted due to context budget" in result
        assert "mem-dropped-1" not in result
        assert "mem-dropped-2" not in result

    def test_format_memory_json_fallback_does_not_add_memory_id(self) -> None:
        """Leaves raw memory tool JSON results on their existing id field."""
        dr = {
            "tool": "list_memories",
            "result": {
                "memories": [
                    {
                        "id": "mm-abc123",
                        "content": "Use task-linked commits.",
                    }
                ]
            },
        }

        result = HookManager._format_discovery_result(dr)

        assert '"id": "mm-abc123"' in result
        assert "memory_id" not in result

    def test_format_recall_review_lessons_for_files(self) -> None:
        """Formats review lesson recall as compact guidance."""
        dr = {
            "tool": "recall_review_lessons_for_files",
            "result": {
                "lessons": [
                    {
                        "memory_id": "mem-1",
                        "pattern_id": "service-config-propagate-db-errors",
                        "matched_file_path": "crates/gcode/src/config/services.rs",
                        "do": "Propagate database read failures.",
                        "avoid": "Collapsing database read failures into None.",
                    }
                ]
            },
        }

        result = HookManager._format_discovery_result(dr)

        assert "<review-guidance>" in result
        assert "service-config-propagate-db-errors" in result
        assert "Do: Propagate database read failures" in result
        assert "Avoid: Collapsing database read failures into None" in result
        assert "```json" not in result

    def test_format_unknown_tool(self) -> None:
        """Formats unknown tool result as JSON."""
        dr = {
            "tool": "some_other_tool",
            "result": {"data": "value"},
        }
        result = HookManager._format_discovery_result(dr)
        assert "some_other_tool" in result
        assert "value" in result


class TestEvaluateWorkflowRules:
    """Tests for _evaluate_workflow_rules."""

    def test_routine_allow_logs_at_debug(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Routine allow decisions log at debug instead of info."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        manager.logger.info.assert_not_called()
        manager.logger.debug.assert_called_once()
        debug_message = manager.logger.debug.call_args[0][0]
        assert "event=before_tool" in debug_message
        assert "decision=allow" in debug_message
        assert "session=session-123" in debug_message
        assert "tool=Read" in debug_message
        assert "mcp_calls=" not in debug_message
        manager._dispatch_mcp_calls.assert_not_called()

    def test_allow_with_dispatch_only_mcp_calls_logs_at_debug(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Routine dispatch-only MCP calls stay debug-level."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            metadata={
                "mcp_calls": [
                    {"server": "gobby-memory", "tool": "search_memories"},
                    {"server": "gobby-tasks", "tool": "list_tasks"},
                ]
            },
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        manager.logger.info.assert_not_called()
        manager.logger.debug.assert_called_once()
        debug_message = manager.logger.debug.call_args[0][0]
        assert "decision=allow" in debug_message
        assert "tool=Write" in debug_message
        assert "mcp_calls=2" in debug_message
        assert "gobby-memory/search_memories" in debug_message
        assert "gobby-tasks/list_tasks" in debug_message
        manager._dispatch_mcp_calls.assert_called_once_with(
            [
                {"server": "gobby-memory", "tool": "search_memories"},
                {"server": "gobby-tasks", "tool": "list_tasks"},
            ],
            event,
        )

    def test_allow_with_captured_mcp_call_logs_at_info(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Captured or blocking MCP calls remain operator-visible."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            metadata={
                "mcp_calls": [
                    {
                        "server": "gobby-memory",
                        "tool": "search_memories",
                        "inject_result": True,
                    }
                ]
            },
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_AGENT, data={"prompt": "use context"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        manager.logger.debug.assert_not_called()
        manager.logger.info.assert_called_once()
        info_message = manager.logger.info.call_args[0][0]
        assert "decision=allow" in info_message
        assert "mcp_calls=1" in info_message
        assert "gobby-memory/search_memories" in info_message

    def test_allow_with_input_rewrite_logs_at_debug(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Routine input rewriting and auto-approve stay debug-level."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            modified_input={"path": "rewritten.txt"},
            auto_approve=True,
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        assert event.metadata["_modified_input"] == {"path": "rewritten.txt"}
        assert event.metadata["_auto_approve"] is True
        manager.logger.info.assert_not_called()
        manager.logger.debug.assert_called_once()
        debug_message = manager.logger.debug.call_args[0][0]
        assert "decision=allow" in debug_message
        assert "rewrote_input=true" in debug_message
        assert "auto_approve=true" in debug_message

    def test_allow_with_user_visible_response_logs_at_info(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Allow decisions with user-visible output remain info-level."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision="allow",
            system_message="Handoff is ready",
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_AGENT, data={"prompt": "status"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None
        manager.logger.debug.assert_not_called()
        manager.logger.info.assert_called_once()
        info_message = manager.logger.info.call_args[0][0]
        assert "decision=allow" in info_message

    @pytest.mark.parametrize("decision", ["block", "deny", "ask"])
    def test_routine_blocking_decisions_log_at_debug_once(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
        decision: str,
    ) -> None:
        """Routine blocking workflow outcomes log once at debug level."""
        manager = manager_with_mocks
        manager.logger = MagicMock()
        manager._workflow_handler.handle.return_value = HookResponse(
            decision=decision,
            reason="Blocked by rule",
        )
        manager._dispatch_mcp_calls = MagicMock(return_value=[])

        event = make_event(event_type=HookEventType.BEFORE_TOOL, data={"tool_name": "Bash"})
        event.metadata["_platform_session_id"] = "session-123"

        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking == HookResponse(decision=decision, reason="Blocked by rule")
        manager.logger.info.assert_not_called()
        manager.logger.debug.assert_called_once()
        debug_message = manager.logger.debug.call_args[0][0]
        assert f"decision={decision}" in debug_message
        assert "reason=Blocked by rule" in debug_message

    def test_advisory_workflow_evaluation_exception_fails_open(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        """Workflow evaluation exceptions fail open for advisory events."""
        manager = manager_with_mocks
        manager._workflow_handler.handle.side_effect = RuntimeError("Workflow engine error")

        event = make_event(event_type=HookEventType.BEFORE_AGENT)
        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking is None

    @pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.STOP_FAILURE])
    def test_stop_workflow_evaluation_exception_fails_closed(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
        event_type: HookEventType,
    ) -> None:
        """Workflow evaluation exceptions block STOP-class events."""
        manager = manager_with_mocks
        manager._workflow_handler.handle.side_effect = RuntimeError("Workflow engine error")

        event = make_event(event_type=event_type)
        context, blocking = manager._evaluate_workflow_rules(event)

        assert context is None
        assert blocking == HookResponse(
            decision="block",
            reason="Workflow evaluation failed; blocking stop for safety.",
        )


class TestShutdown:
    """Tests for shutdown method."""

    def test_shutdown_stops_health_monitor(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """shutdown stops the health monitor."""
        manager = manager_with_mocks
        manager._webhook_dispatcher.close = AsyncMock()

        manager.shutdown()

        manager._health_monitor.stop.assert_called_once()
        assert manager._health_monitor.stop.call_count == 1
        assert manager._health_monitor.stop.call_args is not None

    def test_shutdown_closes_database(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """shutdown closes the database."""
        manager = manager_with_mocks
        manager._webhook_dispatcher.close = AsyncMock()

        manager.shutdown()

        manager._database.close.assert_called_once()
        assert manager._database.close.call_count == 1
        assert manager._database.close.call_args is not None

    def test_shutdown_async_closes_webhook_dispatcher(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """shutdown_async awaits webhook dispatcher close without warning."""
        import asyncio

        async def run_shutdown() -> None:
            manager = manager_with_mocks
            closed = asyncio.Event()

            async def close_dispatcher() -> None:
                closed.set()

            manager._webhook_dispatcher.close = AsyncMock()
            manager._webhook_dispatcher.close.side_effect = close_dispatcher
            manager.logger = MagicMock()

            await manager.shutdown_async()

            assert closed.is_set()
            manager._webhook_dispatcher.close.assert_awaited_once()
            manager.logger.warning.assert_not_called()

        asyncio.run(run_shutdown())

    def test_shutdown_on_current_loop_schedules_webhook_close(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """shutdown avoids blocking when called on the captured event loop."""
        import asyncio

        async def run_shutdown() -> None:
            manager = manager_with_mocks
            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def close_dispatcher() -> None:
                closed.set()

            manager._loop = loop
            manager._webhook_dispatcher.close = AsyncMock()
            manager._webhook_dispatcher.close.side_effect = close_dispatcher
            manager.logger = MagicMock()

            with patch("gobby.hooks.hook_manager.asyncio.get_running_loop", return_value=loop):
                manager.shutdown()
            await asyncio.wait_for(closed.wait(), timeout=1)

            assert closed.is_set()
            manager._webhook_dispatcher.close.assert_awaited_once()
            manager.logger.warning.assert_not_called()

        asyncio.run(run_shutdown())

    def test_shutdown_logs_webhook_close_error_with_details(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """shutdown logs webhook dispatcher close failures with useful detail."""
        manager = manager_with_mocks

        async def failing_close() -> None:
            raise RuntimeError("Close failed")

        manager._webhook_dispatcher.close = failing_close
        manager._loop = None
        manager.logger = MagicMock()

        manager.shutdown()

        manager.logger.warning.assert_called_once()
        warning_call = manager.logger.warning.call_args
        assert warning_call.args == (
            "Failed to close webhook dispatcher (%s): %s",
            "RuntimeError",
            "Close failed",
        )
        assert warning_call.kwargs == {"exc_info": True}


class TestRunCoroBlocking:
    """Tests for _run_coro_blocking helper."""

    def test_run_coro_blocking_with_no_loop(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_run_coro_blocking uses asyncio.run when no loop is running."""
        manager = manager_with_mocks
        manager._loop = None

        async def sample_coro() -> str:
            return "result"

        result = manager._run_coro_blocking(sample_coro())
        assert result == "result"

    def test_run_coro_blocking_handles_error(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_run_coro_blocking logs exception details and returns None on error."""
        manager = manager_with_mocks
        manager._loop = None
        manager.logger = MagicMock()

        async def failing_coro() -> str:
            raise RuntimeError("fail")

        result = manager._run_coro_blocking(failing_coro())
        assert result is None
        manager.logger.exception.assert_called_once()
        log_args = manager.logger.exception.call_args.args
        assert log_args[:3] == (
            "run_coro_blocking%s: asyncio.run failed: %s: %s",
            "",
            "RuntimeError",
        )
        assert str(log_args[3]) == "fail"
        assert manager.logger.exception.call_args.kwargs == {}

    def test_run_coro_blocking_forwards_label_and_timeout(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_run_coro_blocking forwards dispatcher diagnostics and timeout settings."""
        manager = manager_with_mocks
        coro = object()

        with patch(
            "gobby.hooks.hook_manager.mcp_dispatcher.run_coro_blocking",
            return_value="result",
        ) as run_coro:
            result = manager._run_coro_blocking(
                coro,
                label="before_agent:memory_recall",
                timeout_seconds=65,
            )

        assert result == "result"
        run_coro.assert_called_once_with(
            coro,
            manager._loop,
            manager.logger,
            label="before_agent:memory_recall",
            timeout_seconds=65,
        )

    def test_run_coro_blocking_timeout_logs_label_and_cancels_future(self) -> None:
        """Thread-safe dispatch timeout should be labelled and cancel the scheduled work."""
        loop = asyncio.new_event_loop()
        loop_started = threading.Event()
        coro_cancelled = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop_started.set()
            loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        assert loop_started.wait(timeout=1)

        async def slow_coro() -> str:
            pending: asyncio.Future[str] = asyncio.Future()
            try:
                await pending
            except asyncio.CancelledError:
                coro_cancelled.set()
                raise
            return "done"

        logger = MagicMock()
        label = "session_start:gobby-sessions/capture_baseline_dirty_files"

        try:
            result = run_coro_blocking(
                slow_coro(),
                loop,
                logger,
                label=label,
                timeout_seconds=0.01,
            )

            assert result is None
            logger.exception.assert_called_once()
            log_args = logger.exception.call_args.args
            assert log_args[:4] == (
                "run_coro_blocking%s: threadsafe failed after %ss: %s: %s",
                f"[{label}]",
                0.01,
                "TimeoutError",
            )
            assert isinstance(log_args[4], TimeoutError)
            assert logger.exception.call_args.kwargs == {}
            assert coro_cancelled.wait(timeout=1)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=1)
            loop.close()


class TestEnsureProjectInDb:
    """Tests for _ensure_project_in_db."""

    def test_ensure_project_no_session_manager(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_ensure_project_in_db does nothing when session_manager is None."""
        manager = manager_with_mocks
        manager._session_manager = None

        manager._ensure_project_in_db({"id": "proj-1", "name": "test"})
        assert manager._session_manager is None

    def test_ensure_project_db_error_handled(
        self,
        manager_with_mocks: HookManager,
    ) -> None:
        """_ensure_project_in_db handles DB errors gracefully."""
        manager = manager_with_mocks
        manager._session_manager = MagicMock()
        manager._session_manager.db = MagicMock()

        with patch("gobby.storage.projects.LocalProjectManager") as MockPM:
            MockPM.return_value.ensure_exists.side_effect = ValueError("DB error")
            result = manager._ensure_project_in_db({"id": "proj-1", "name": "test"})
            assert result is None
            MockPM.return_value.ensure_exists.assert_called_once_with("proj-1", "test", None)


class TestSessionManagerUnification:
    """Tests for canonical session manager wiring inside HookManager."""

    def test_init_keeps_only_canonical_session_manager(
        self,
        manager_with_mocks: HookManager,
        mock_components: MagicMock,
    ) -> None:
        """HookManager should expose only the canonical session manager handle."""
        manager = manager_with_mocks

        assert manager._session_manager is mock_components.session_manager
        assert not hasattr(manager, "_session_storage")


# ─── Tests for _resolve_session_refs_in_tool_input ─────────────────────


class TestResolveSessionRefsInToolInput:
    """Tests for #N → UUID resolution at the hook boundary."""

    def test_resolves_top_level_session_id(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """#N in call_tool wrapper session_id resolves without requesting modified_input."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.return_value = "uuid-abc-123"

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#4"},
                    "session_id": "#3",
                },
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "uuid-abc-123"
        assert "_session_refs_resolved" not in event.metadata
        manager._session_manager.resolve_session_reference.assert_called_once_with("#3", "proj-1")

    def test_preserves_top_level_session_id_for_set_variable(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """set_variable keeps #N so the tool can resolve it itself."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {"name": "flag", "value": True, "session_id": "#3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "#3"
        assert "_session_refs_resolved" not in event.metadata
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_preserves_top_level_session_id_for_get_variable(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """get_variable keeps #N so the tool can resolve it itself."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__get_variable",
                "tool_input": {"name": "flag", "session_id": "#3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "#3"
        assert "_session_refs_resolved" not in event.metadata
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_resolves_nested_call_tool_arguments(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """#N inside call_tool arguments.session_id is resolved."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.return_value = "uuid-def-456"

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#4", "session_id": "#3"},
                },
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["arguments"]["session_id"] == "uuid-def-456"
        assert "_session_refs_resolved" not in event.metadata

    def test_uuid_passthrough_no_rewrite(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """UUID session_id is not rewritten."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {
                    "name": "flag",
                    "value": True,
                    "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                },
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert "_session_refs_resolved" not in event.metadata
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_skips_non_mcp_tools(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Non-MCP tools are not touched."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello", "session_id": "#3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "#3"
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_skips_non_before_tool_events(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Only BEFORE_TOOL events trigger resolution."""
        manager = manager_with_mocks

        event = make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {"session_id": "#3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "#3"
        manager._session_manager.resolve_session_reference.assert_not_called()

    def test_resolution_error_is_swallowed(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Resolution errors are logged and don't crash the hook."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.side_effect = ValueError(
            "Session #99 not found"
        )

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {"session_id": "#99"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        # session_id unchanged on error
        assert event.data["tool_input"]["session_id"] == "#99"
        assert "_session_refs_resolved" not in event.metadata

    def test_numeric_string_without_hash(
        self, manager_with_mocks: HookManager, make_event: Callable
    ) -> None:
        """Variable tools preserve plain numeric refs for internal resolution."""
        manager = manager_with_mocks
        manager._session_manager.resolve_session_reference.return_value = "uuid-789"

        event = make_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__get_variable",
                "tool_input": {"session_id": "3"},
            },
        )
        event.project_id = "proj-1"

        manager._resolve_session_refs_in_tool_input(event)

        assert event.data["tool_input"]["session_id"] == "3"
        assert "_session_refs_resolved" not in event.metadata
        manager._session_manager.resolve_session_reference.assert_not_called()


class TestRecordSessionActivityPulse:
    """Activity pulses from hook events feed the statusline gap detector."""

    def test_non_session_start_records_activity_after_session_lookup(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        from gobby.servers.routes.sessions import statusline_activity

        manager = manager_with_mocks
        statusline_activity.reset_for_tests()

        def resolve(event: HookEvent) -> None:
            event.metadata["_platform_session_id"] = "platform-abc"

        manager._session_lookup.resolve.side_effect = resolve
        manager._event_handlers.get_handler.return_value = MagicMock(
            return_value=HookResponse(decision="allow")
        )
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_AGENT)
        manager._handle_internal(event)

        assert statusline_activity.last_session_activity("platform-abc") is not None

    def test_session_start_records_activity_after_handler(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        from gobby.servers.routes.sessions import statusline_activity

        manager = manager_with_mocks
        statusline_activity.reset_for_tests()

        def handler(event: HookEvent) -> HookResponse:
            event.metadata["_platform_session_id"] = "platform-xyz"
            return HookResponse(decision="allow")

        manager._event_handlers.get_handler.return_value = handler
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()
        manager._resolve_project_id = MagicMock(return_value=PERSONAL_PROJECT_ID)

        event = make_event(event_type=HookEventType.SESSION_START, data={"cwd": "/tmp/p"})
        manager._handle_internal(event)

        assert statusline_activity.last_session_activity("platform-xyz") is not None

    def test_no_activity_recorded_when_platform_id_missing(
        self,
        manager_with_mocks: HookManager,
        make_event: Callable,
    ) -> None:
        from gobby.servers.routes.sessions import statusline_activity

        manager = manager_with_mocks
        statusline_activity.reset_for_tests()

        manager._session_lookup.resolve.return_value = None
        manager._event_handlers.get_handler.return_value = MagicMock(
            return_value=HookResponse(decision="allow")
        )
        manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
        manager._enricher.enrich = MagicMock()

        event = make_event(event_type=HookEventType.BEFORE_AGENT)
        manager._handle_internal(event)

        assert statusline_activity.last_session_activity("platform-abc") is None
