"""Tests for SafeExpressionEvaluator with ConditionEvaluator helper functions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit


# --- Fixtures ---


@pytest.fixture
def mock_task_manager() -> MagicMock:
    """Create a mock task manager."""
    tm = MagicMock()
    return tm


@pytest.fixture
def mock_stop_registry() -> MagicMock:
    """Create a mock stop registry."""
    reg = MagicMock()
    reg.has_pending_signal.return_value = False
    return reg


def _make_task(status: str = "open", requires_user_review: bool = False) -> MagicMock:
    """Create a mock task with given status."""
    task = MagicMock()
    task.status = status
    task.requires_user_review = requires_user_review
    return task


def _build_evaluator(
    context: dict[str, Any],
    task_manager: Any = None,
    stop_registry: Any = None,
    plugin_conditions: dict[str, Any] | None = None,
) -> SafeExpressionEvaluator:
    """Build an evaluator with condition helpers wired up."""
    helpers = build_condition_helpers(
        task_manager=task_manager,
        stop_registry=stop_registry,
        plugin_conditions=plugin_conditions,
        context=context,
    )
    return SafeExpressionEvaluator(context, helpers)


# --- task_tree_complete tests ---


class TestTaskTreeComplete:
    def test_returns_true_when_task_id_is_none(self, mock_task_manager: MagicMock) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_tree_complete(None)") is True

    def test_returns_true_when_task_closed(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_tree_complete('task-123')") is True

    def test_returns_false_when_task_open(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="open")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_tree_complete('task-123')") is False

    def test_returns_false_when_subtask_open(self, mock_task_manager: MagicMock) -> None:
        parent = _make_task(status="closed")
        child = _make_task(status="open")
        child.id = "child-1"

        mock_task_manager.get_task.side_effect = lambda task_id: (
            parent if task_id == "task-123" else child
        )
        mock_task_manager.list_tasks.side_effect = lambda parent_task_id: (
            [child] if parent_task_id == "task-123" else []
        )

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_tree_complete('task-123')") is False

    def test_no_task_manager_returns_true(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=None)
        # Without task_manager, should return True (no-op, matches ConditionEvaluator behavior)
        assert ev.evaluate("task_tree_complete('task-123')") is True


# --- task_needs_user_review tests ---


class TestTaskNeedsUserReview:
    def test_returns_true_when_needs_review(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="needs_review", requires_user_review=True)
        mock_task_manager.get_task.return_value = task

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_needs_user_review('task-123')") is True

    def test_returns_false_when_not_needs_review(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="open")
        mock_task_manager.get_task.return_value = task

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_needs_user_review('task-123')") is False

    def test_returns_false_when_none(self, mock_task_manager: MagicMock) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_needs_user_review(None)") is False

    def test_no_task_manager_returns_false(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=None)
        assert ev.evaluate("task_needs_user_review('task-123')") is False


# --- has_stop_signal tests ---


class TestHasStopSignal:
    def test_returns_true_when_signal_pending(self, mock_stop_registry: MagicMock) -> None:
        mock_stop_registry.has_pending_signal.return_value = True

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, stop_registry=mock_stop_registry)
        assert ev.evaluate("has_stop_signal('session-abc')") is True
        mock_stop_registry.has_pending_signal.assert_called_once_with("session-abc")

    def test_returns_false_when_no_signal(self, mock_stop_registry: MagicMock) -> None:
        mock_stop_registry.has_pending_signal.return_value = False

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, stop_registry=mock_stop_registry)
        assert ev.evaluate("has_stop_signal('session-abc')") is False

    def test_no_stop_registry_returns_false(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, stop_registry=None)
        assert ev.evaluate("has_stop_signal('session-abc')") is False


# --- mcp_called tests ---


class TestMcpCalled:
    def test_returns_true_when_server_called(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_calls": {"gobby-tasks": ["create_task", "claim_task"]}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_called('gobby-tasks')") is True

    def test_returns_true_when_specific_tool_called(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_calls": {"gobby-tasks": ["create_task", "claim_task"]}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_called('gobby-tasks', 'claim_task')") is True

    def test_returns_false_when_tool_not_called(self) -> None:
        ctx: dict[str, Any] = {"variables": {"mcp_calls": {"gobby-tasks": ["create_task"]}}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_called('gobby-tasks', 'close_task')") is False

    def test_returns_false_when_server_not_called(self) -> None:
        ctx: dict[str, Any] = {"variables": {"mcp_calls": {}}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_called('gobby-memory')") is False

    def test_returns_false_when_no_mcp_calls(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_called('gobby-tasks')") is False


# --- mcp_result_is_null tests ---


class TestMcpResultIsNull:
    def test_returns_true_when_result_is_none(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-tasks": {"suggest_next_task": None}}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_result_is_null('gobby-tasks', 'suggest_next_task')") is True

    def test_returns_false_when_result_exists(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-tasks": {"suggest_next_task": {"ref": "#123"}}}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_result_is_null('gobby-tasks', 'suggest_next_task')") is False

    def test_returns_true_when_no_results(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_result_is_null('gobby-tasks', 'suggest_next_task')") is True

    def test_returns_true_when_server_not_in_results(self) -> None:
        ctx: dict[str, Any] = {"variables": {"mcp_results": {}}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_result_is_null('gobby-tasks', 'suggest_next_task')") is True


# --- mcp_failed tests ---


class TestMcpFailed:
    def test_returns_true_when_success_false(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {
                "mcp_results": {
                    "gobby-agents": {"spawn_agent": {"success": False, "error": "fail"}}
                }
            }
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_failed('gobby-agents', 'spawn_agent')") is True

    def test_returns_true_when_error_present(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {
                "mcp_results": {"gobby-agents": {"spawn_agent": {"error": "something broke"}}}
            }
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_failed('gobby-agents', 'spawn_agent')") is True

    def test_returns_true_when_status_failed(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-agents": {"spawn_agent": {"status": "failed"}}}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_failed('gobby-agents', 'spawn_agent')") is True

    def test_returns_false_when_success(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-agents": {"spawn_agent": {"success": True}}}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_failed('gobby-agents', 'spawn_agent')") is False

    def test_returns_false_when_no_result(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_failed('gobby-agents', 'spawn_agent')") is False


# --- mcp_result_has tests ---


class TestMcpResultHas:
    def test_returns_true_when_field_matches(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {
                "mcp_results": {
                    "gobby-tasks": {"wait_for_task": {"timed_out": True, "result": "ok"}}
                }
            }
        }
        ev = _build_evaluator(ctx)
        assert (
            ev.evaluate("mcp_result_has('gobby-tasks', 'wait_for_task', 'timed_out', True)") is True
        )

    def test_returns_false_when_field_doesnt_match(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-tasks": {"wait_for_task": {"timed_out": False}}}}
        }
        ev = _build_evaluator(ctx)
        assert (
            ev.evaluate("mcp_result_has('gobby-tasks', 'wait_for_task', 'timed_out', True)")
            is False
        )

    def test_returns_false_when_no_result(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx)
        assert (
            ev.evaluate("mcp_result_has('gobby-tasks', 'wait_for_task', 'timed_out', True)")
            is False
        )

    def test_string_value_match(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-tasks": {"get_task": {"status": "closed"}}}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_result_has('gobby-tasks', 'get_task', 'status', 'closed')") is True


# --- Plugin conditions tests ---


class TestPluginConditions:
    def test_plugin_condition_callable(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        plugin_conditions = {"plugin_my_plugin_passes_lint": lambda: True}
        ev = _build_evaluator(ctx, plugin_conditions=plugin_conditions)
        assert ev.evaluate("plugin_my_plugin_passes_lint()") is True

    def test_plugin_condition_returns_false(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        plugin_conditions = {"plugin_my_plugin_passes_lint": lambda: False}
        ev = _build_evaluator(ctx, plugin_conditions=plugin_conditions)
        assert ev.evaluate("plugin_my_plugin_passes_lint()") is False


# --- Integration: combined expressions ---


class TestCombinedExpressions:
    def test_boolean_and_with_helpers(self, mock_task_manager: MagicMock) -> None:
        """Test combining task helpers with boolean logic."""
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        ctx: dict[str, Any] = {
            "variables": {"mcp_calls": {"gobby-tasks": ["claim_task"]}},
        }
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert (
            ev.evaluate(
                "task_tree_complete('task-123') and mcp_called('gobby-tasks', 'claim_task')"
            )
            is True
        )

    def test_negation_with_helpers(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("not mcp_called('gobby-tasks')") is True

    def test_or_returns_actual_value_not_bool(self) -> None:
        """Python's `or` returns actual values — needed for (dict.get() or {}).get()."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {"a": None, "b": {"key": "val"}}
        ev = SafeExpressionEvaluator(ctx, {"len": len})
        # `None or {'key': 'val'}` should return the dict, not True
        assert ev.evaluate("(a or b).get('key') == 'val'") is True

    def test_and_returns_actual_value_not_bool(self) -> None:
        """Python's `and` returns last truthy or first falsy."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {"a": "hello", "b": ""}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("a and b") is False  # b is falsy empty string; evaluate() returns bool

    def test_chained_or_default_pattern(self) -> None:
        """Test the (dict.get('key') or {}).get('nested') pattern from lifecycle YAML."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {
            "event": {"data": {"tool_input": {"arguments": {"commit_sha": "abc"}}}}
        }
        ev = SafeExpressionEvaluator(ctx, {})
        # This is the pattern from session-lifecycle.yaml line 363
        result = ev.evaluate(
            "((event.data.get('tool_input') or {}).get('arguments') or {}).get('commit_sha')"
        )
        assert result is True  # evaluate() wraps result in bool(); "abc" is truthy

    def test_string_strip_method(self) -> None:
        """Test .strip() on strings — used in lifecycle title synthesis."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {"s": "  hello  "}
        ev = SafeExpressionEvaluator(ctx, {"len": len})
        assert ev.evaluate("len(s.strip()) > 0") is True

    def test_string_startswith_method(self) -> None:
        """Test .startswith() — used in lifecycle YAML to detect slash commands."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {"prompt": "/gobby help"}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("prompt.startswith('/')") is True

        ctx2: dict[str, Any] = {"prompt": "help me"}
        ev2 = SafeExpressionEvaluator(ctx2, {})
        assert ev2.evaluate("prompt.startswith('/')") is False

    def test_lifecycle_title_synthesis_expression(self) -> None:
        """Test the exact expression from session-lifecycle.yaml for title synthesis."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {"event": {"data": {"prompt": "Fix the login bug"}}}
        ev = SafeExpressionEvaluator(ctx, {"len": len})
        expr = "len((event.data.get('prompt') or '').strip()) >= 10 and not (event.data.get('prompt') or '').strip().startswith('/')"
        assert ev.evaluate(expr) is True

        # Slash command should fail
        ctx2: dict[str, Any] = {"event": {"data": {"prompt": "/gobby help with tasks"}}}
        ev2 = SafeExpressionEvaluator(ctx2, {"len": len})
        assert ev2.evaluate(expr) is False

        # Short prompt should fail
        ctx3: dict[str, Any] = {"event": {"data": {"prompt": "hi"}}}
        ev3 = SafeExpressionEvaluator(ctx3, {"len": len})
        assert ev3.evaluate(expr) is False

    def test_helper_with_variable_reference(self, mock_task_manager: MagicMock) -> None:
        """Test calling a helper with a variable from context."""
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        ctx: dict[str, Any] = {
            "variables": {"session_task": "task-456"},
            "session_task": "task-456",  # Flattened into context
        }
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        # This simulates: task_tree_complete(variables.session_task)
        assert ev.evaluate("task_tree_complete(session_task)") is True


# ═══════════════════════════════════════════════════════════════════════
# _normalize_expr — whitespace normalization
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeExpr:
    """Verify _normalize_expr collapses YAML folding artefacts."""

    def test_collapses_newline_with_indent(self) -> None:
        raw = "(a + b)\n  not in c"
        assert SafeExpressionEvaluator._normalize_expr(raw) == "(a + b) not in c"

    def test_collapses_multiple_newlines(self) -> None:
        raw = "a\n  and b\n  and c"
        assert SafeExpressionEvaluator._normalize_expr(raw) == "a and b and c"

    def test_preserves_single_spaces(self) -> None:
        raw = "a and b not in c"
        assert SafeExpressionEvaluator._normalize_expr(raw) == "a and b not in c"

    def test_strips_trailing_newline(self) -> None:
        raw = "a and b\n"
        assert SafeExpressionEvaluator._normalize_expr(raw) == "a and b"

    def test_evaluate_with_yaml_folding_artefact(self) -> None:
        """Reproduce the actual bug: YAML > preserves newline before 'not in'."""
        expr_with_newline = (
            "variables.get('allowed') "
            "and (variables.get('x') + ':' + variables.get('y'))\n"
            "  not in variables.get('allowed', [])"
        )
        ctx: dict[str, Any] = {
            "variables": {"allowed": ["a:b"], "x": "a", "y": "c"},
        }
        ev = SafeExpressionEvaluator(ctx, {"len": len})
        # Without normalization this would raise ValueError (SyntaxError from ast.parse)
        assert ev.evaluate(expr_with_newline) is True  # "a:c" not in ["a:b"]

    def test_evaluate_with_yaml_folding_artefact_allowed(self) -> None:
        """Same expression but the value IS in the list — should be False."""
        expr_with_newline = (
            "variables.get('allowed') "
            "and (variables.get('x') + ':' + variables.get('y'))\n"
            "  not in variables.get('allowed', [])"
        )
        ctx: dict[str, Any] = {
            "variables": {"allowed": ["a:b"], "x": "a", "y": "b"},
        }
        ev = SafeExpressionEvaluator(ctx, {"len": len})
        assert ev.evaluate(expr_with_newline) is False  # "a:b" in ["a:b"]
