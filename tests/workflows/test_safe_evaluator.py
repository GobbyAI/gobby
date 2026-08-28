"""Tests for SafeExpressionEvaluator with ConditionEvaluator helper functions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.tool_error_tracker import extract_target_key
from gobby.workflows.safe_evaluator import (
    ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS,
    ASSISTANT_RESPONSE_SCAN_LIMIT,
    LazyBool,
    SafeExpressionEvaluator,
    build_condition_helpers,
)

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
    reg.acknowledge.return_value = False
    return reg


def _make_task(*, closed: bool = False, stage_state: str = "ready") -> MagicMock:
    """Create a mock task with canonical projected-state fields."""
    task = MagicMock()
    task.closed_at = "2024-01-02T00:00:00Z" if closed else None
    task.current_stage = {"state": "done" if closed else stage_state}
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


class TestLazyBool:
    @pytest.mark.parametrize(("value", "literal"), [(True, "true"), (False, "false")])
    def test_equality_and_inequality_evaluate_once(self, value: bool, literal: str) -> None:
        calls = 0

        def thunk() -> bool:
            nonlocal calls
            calls += 1
            return value

        evaluator = SafeExpressionEvaluator({"value": LazyBool(thunk)}, {})

        assert evaluator.evaluate(f"value == {literal}") is True
        assert evaluator.evaluate(f"value != {literal}") is False
        assert calls == 1

    def test_lazy_values_compare_by_boolean_value(self) -> None:
        assert LazyBool(lambda: True) == LazyBool(lambda: True)
        assert LazyBool(lambda: False) != LazyBool(lambda: True)

    def test_hash_and_containment_use_cached_boolean_value(self) -> None:
        calls = 0

        def thunk() -> bool:
            nonlocal calls
            calls += 1
            return True

        value = LazyBool(thunk)
        evaluator = SafeExpressionEvaluator({"value": value}, {})

        assert evaluator.evaluate("value in [true, false]") is True
        assert hash(value) == hash(True)
        assert value in {True}
        assert calls == 1


# --- task_tree_complete tests ---


class TestTaskTreeComplete:
    def test_returns_true_when_task_id_is_none(self, mock_task_manager: MagicMock) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_tree_complete(None)") is True

    def test_returns_true_when_task_closed(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(closed=True)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_tree_complete('task-123')") is True

    def test_returns_false_when_task_open(self, mock_task_manager: MagicMock) -> None:
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)
        assert ev.evaluate("task_tree_complete('task-123')") is False

    def test_returns_false_when_subtask_open(self, mock_task_manager: MagicMock) -> None:
        parent = _make_task(closed=True)
        # _is_tree_complete resolves subtasks via the task row's own id
        # (list_tasks(parent_task_id=task.id)), so the mock id must match the ref.
        parent.id = "task-123"
        child = _make_task()
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

    def test_no_task_manager_returns_false(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=None)
        assert ev.evaluate("task_tree_complete('task-123')") is False


class TestTaskTypeIn:
    def test_task_type_in_helper_is_available(self, mock_task_manager: MagicMock) -> None:
        task = _make_task()
        task.task_type = "epic"
        mock_task_manager.get_task.return_value = task

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=mock_task_manager)

        assert ev.evaluate("task_type_in(['task-123'], 'epic')") is True

    def test_task_type_in_without_manager_returns_false(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, task_manager=None)

        assert ev.evaluate("task_type_in(['task-123'], 'epic')") is False


# --- has_stop_signal tests ---


class TestHasStopSignal:
    def test_returns_true_when_signal_pending(self, mock_stop_registry: MagicMock) -> None:
        mock_stop_registry.has_pending_signal.return_value = True

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, stop_registry=mock_stop_registry)
        assert ev.evaluate("has_stop_signal('session-abc')") is True
        mock_stop_registry.has_pending_signal.assert_called_once_with("session-abc")
        mock_stop_registry.acknowledge.assert_not_called()

    def test_returns_false_when_no_signal(self, mock_stop_registry: MagicMock) -> None:
        mock_stop_registry.has_pending_signal.return_value = False

        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx, stop_registry=mock_stop_registry)
        assert ev.evaluate("has_stop_signal('session-abc')") is False
        mock_stop_registry.has_pending_signal.assert_called_once_with("session-abc")
        mock_stop_registry.acknowledge.assert_not_called()

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


class TestToolCallSucceeded:
    def _eval(
        self,
        data: dict[str, Any],
        metadata: Any = None,
    ) -> SafeExpressionEvaluator:
        ctx: dict[str, Any] = {
            "variables": {},
            "event": SimpleNamespace(data=data, metadata=metadata or {}),
        }
        return _build_evaluator(ctx)

    def test_returns_true_for_successful_tool_call(self) -> None:
        ev = self._eval({"tool_outcome": {"status": "succeeded"}})

        assert ev.evaluate("tool_call_succeeded()") is True

    @pytest.mark.parametrize("status", ["failed", "unknown"])
    def test_returns_false_without_successful_outcome(self, status: str) -> None:
        ev = self._eval({"tool_outcome": {"status": status}})

        assert ev.evaluate("tool_call_succeeded()") is False

    @pytest.mark.parametrize("data", [{}, {"tool_output": None}])
    def test_rejects_missing_outcome(self, data: dict[str, Any]) -> None:
        ev = self._eval(data)

        assert ev.evaluate("tool_call_succeeded()") is False

    def test_accepts_structured_success_alias(self) -> None:
        ev = self._eval({"tool_output": {"success": True, "result": {"id": "ok"}}})

        assert ev.evaluate("tool_call_succeeded()") is True

    def test_rejects_top_level_is_error(self) -> None:
        ev = self._eval({"is_error": True, "tool_output": {"success": True}})

        assert ev.evaluate("tool_call_succeeded()") is False

    def test_ignores_human_readable_error_text(self) -> None:
        ev = self._eval({"error": "tool failed", "tool_output": {"success": True}})

        assert ev.evaluate("tool_call_succeeded()") is True

    def test_rejects_failure_metadata(self) -> None:
        ev = self._eval({"tool_output": {"success": True}}, metadata={"is_failure": True})

        assert ev.evaluate("tool_call_succeeded()") is False

    def test_handles_dict_event_shape(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {},
            "event": {
                "data": {"tool_outcome": {"status": "succeeded"}},
                "metadata": {},
            },
        }
        ev = _build_evaluator(ctx)

        assert ev.evaluate("tool_call_succeeded()") is True

    def test_handles_missing_or_malformed_event_safely(self) -> None:
        class BrokenEvent:
            @property
            def data(self) -> dict[str, Any]:
                raise AttributeError("bad event")

        assert (
            _build_evaluator({"variables": {}, "event": None}).evaluate("tool_call_succeeded()")
            is False
        )
        assert (
            _build_evaluator({"variables": {}, "event": BrokenEvent()}).evaluate(
                "tool_call_succeeded()"
            )
            is False
        )

    def test_direct_success_precedes_nested_structured_error_result(self) -> None:
        ev = self._eval({"tool_output": {"success": True, "result": {"isError": True}}})

        assert ev.evaluate("tool_call_succeeded()") is True

    def test_direct_success_precedes_nested_mcp_failure(self) -> None:
        ev = self._eval(
            {"tool_output": {"success": True, "result": {"success": False, "error": "bad"}}}
        )

        assert ev.evaluate("tool_call_succeeded()") is True

    def test_rejects_nested_nonzero_exit_code(self) -> None:
        ev = self._eval({"tool_output": {"result": {"exitCode": 2}}})

        assert ev.evaluate("tool_call_succeeded()") is False

    def test_generic_completed_status_is_unknown(self) -> None:
        ev = self._eval({"tool_output": {"status": "completed"}})

        assert ev.evaluate("tool_call_succeeded()") is False


# --- mcp_result_has tests ---


class TestMcpResultHas:
    def test_returns_true_when_field_matches(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {
                "mcp_results": {
                    "gobby-sessions": {"get_handoff": {"completed": True, "result": "ok"}}
                }
            }
        }
        ev = _build_evaluator(ctx)
        assert (
            ev.evaluate("mcp_result_has('gobby-sessions', 'get_handoff', 'completed', True)")
            is True
        )

    def test_returns_false_when_field_doesnt_match(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-sessions": {"get_handoff": {"completed": False}}}}
        }
        ev = _build_evaluator(ctx)
        assert (
            ev.evaluate("mcp_result_has('gobby-sessions', 'get_handoff', 'completed', True)")
            is False
        )

    def test_returns_false_when_no_result(self) -> None:
        ctx: dict[str, Any] = {"variables": {}}
        ev = _build_evaluator(ctx)
        assert (
            ev.evaluate("mcp_result_has('gobby-sessions', 'get_handoff', 'completed', True)")
            is False
        )

    def test_string_value_match(self) -> None:
        ctx: dict[str, Any] = {
            "variables": {"mcp_results": {"gobby-tasks": {"get_task": {"status": "closed"}}}}
        }
        ev = _build_evaluator(ctx)
        assert ev.evaluate("mcp_result_has('gobby-tasks', 'get_task', 'status', 'closed')") is True


# --- skill_loaded tests ---


class TestSkillLoaded:
    def test_returns_true_when_loaded_skill_present(self) -> None:
        ctx: dict[str, Any] = {"variables": {"loaded_skills": ["python"]}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("skill_loaded('python')") is True

    def test_returns_false_when_only_legacy_injected_skill_present(self) -> None:
        ctx: dict[str, Any] = {"variables": {"injected_skills": ["python"]}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("skill_loaded('python')") is False

    def test_returns_false_when_skill_missing(self) -> None:
        ctx: dict[str, Any] = {"variables": {"loaded_skills": ["rust"]}}
        ev = _build_evaluator(ctx)
        assert ev.evaluate("skill_loaded('python')") is False


# --- has_open_tool_error tests ---


class TestHasOpenToolError:
    @staticmethod
    def _record(
        tool: str = "gobby-skills/get_skill",
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target_arguments = arguments or {"name": "code-index"}
        timestamp = "2026-07-26T12:00:00+00:00"
        return {
            "tool": tool,
            "target_key": extract_target_key(
                {"tool_name": tool.rpartition("/")[2] or tool},
                target_arguments,
            ),
            "error": "Workflow evaluation timed out after 15s",
            "first_at": timestamp,
            "last_at": timestamp,
            "count": 1,
        }

    def test_matches_exact_tool_and_arguments_only(self) -> None:
        context = {
            "variables": {
                "open_tool_errors": [
                    "malformed",
                    self._record(),
                ]
            }
        }
        evaluator = _build_evaluator(context)

        assert evaluator.evaluate(
            'has_open_tool_error("gobby-skills/get_skill", {"name": "code-index"})'
        )
        assert not evaluator.evaluate(
            'has_open_tool_error("gobby-skills/get_skill", {"name": "brevity"})'
        )
        assert not evaluator.evaluate(
            'has_open_tool_error("gobby-skills/search_skills", {"name": "code-index"})'
        )

    @pytest.mark.parametrize(
        "stored_state",
        [
            None,
            {},
            ["malformed"],
            [{"tool": "gobby-skills/get_skill"}],
        ],
    )
    def test_tolerates_malformed_stored_state(self, stored_state: Any) -> None:
        evaluator = _build_evaluator({"variables": {"open_tool_errors": stored_state}})

        assert not evaluator.evaluate(
            'has_open_tool_error("gobby-skills/get_skill", {"name": "code-index"})'
        )


# --- assistant_response_matches_any tests ---


class TestAssistantResponseMatchesAny:
    def _eval(
        self,
        data: dict[str, Any],
        patterns: list[str] | None = None,
    ) -> SafeExpressionEvaluator:
        ctx = {
            "variables": {},
            "event": SimpleNamespace(data=data),
            "patterns": patterns or [],
        }
        return _build_evaluator(ctx)

    def test_empty_response_and_log_return_none(self) -> None:
        ev = self._eval({"response": "", "log": ""})
        assert ev.evaluate_value("assistant_response_matches_any(['In summary'])") is None

    def test_no_match_returns_none(self) -> None:
        ev = self._eval({"response": "Fixed. Tests pass."})
        assert ev.evaluate_value("assistant_response_matches_any(['In summary'])") is None

    def test_literal_match_returns_original_text(self) -> None:
        ev = self._eval({"response": "In summary, fixed."})
        assert ev.evaluate_value("assistant_response_matches_any(['In summary'])") == "In summary"

    def test_literal_match_is_case_insensitive(self) -> None:
        ev = self._eval({"response": "in SUMMARY, fixed."})
        assert ev.evaluate_value("assistant_response_matches_any(['In summary'])") == "in SUMMARY"

    def test_multiple_patterns_use_pattern_order(self) -> None:
        ev = self._eval({"response": "Certainly fixed. In summary, done."})
        assert (
            ev.evaluate_value("assistant_response_matches_any(['In summary', 'Certainly'])")
            == "In summary"
        )

    def test_log_fallback_matches_literal(self) -> None:
        ev = self._eval({"response": "", "log": "Certainly fixed."})
        assert ev.evaluate_value("assistant_response_matches_any(['Certainly'])") == "Certainly"

    def test_regex_match_uses_bundled_patterns(self) -> None:
        ev = self._eval(
            {"response": "This is not a workaround, but a fix."},
            list(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS),
        )
        assert ev.evaluate_value("assistant_response_matches_any(patterns, regex=true)") == (
            "not a workaround, but a fix"
        )

    def test_regex_skips_non_bundled_patterns(self) -> None:
        ev = self._eval({"response": "This is not a workaround, but a fix."}, [r"not .* but"])
        assert ev.evaluate_value("assistant_response_matches_any(patterns, regex=true)") is None

    def test_regex_scan_is_bounded(self) -> None:
        response = "A" * (ASSISTANT_RESPONSE_SCAN_LIMIT + 20) + " not a hack, but a fix"
        ev = self._eval({"response": response}, list(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS))
        assert ev.evaluate_value("assistant_response_matches_any(patterns, regex=true)") is None

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ("This is not a shortcut, but a repair.", "not a shortcut, but a repair"),
            (
                "This is not a hack, not a shortcut, but a repair.",
                "not a hack, not a shortcut, but a repair",
            ),
            ("Use the parser, not string slicing.", "Use the parser, not string slicing"),
        ],
    )
    def test_contrastive_patterns_match_expected_forms(self, response: str, expected: str) -> None:
        ev = self._eval({"response": response}, list(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS))
        assert ev.evaluate_value("assistant_response_matches_any(patterns, regex=true)") == expected

    def test_contrastive_patterns_skip_unrelated_not_usage(self) -> None:
        ev = self._eval(
            {"response": "Do not stop until verification completes."},
            list(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS),
        )
        assert ev.evaluate_value("assistant_response_matches_any(patterns, regex=true)") is None


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


# --- Lowercase boolean/none constants (YAML/JSON convention) ---


class TestLowercaseConstants:
    """Test that lowercase true/false/none from YAML/JSON are accepted."""

    def test_lowercase_true(self) -> None:
        ctx: dict[str, Any] = {"x": 1}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("true") is True

    def test_lowercase_false(self) -> None:
        ctx: dict[str, Any] = {"x": 1}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("false") is False

    def test_lowercase_none(self) -> None:
        ctx: dict[str, Any] = {"x": 1}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate_value("none") is None

    def test_lowercase_in_condition(self) -> None:
        ctx: dict[str, Any] = {"x": None}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("x == none") is True

    def test_lowercase_false_in_condition(self) -> None:
        ctx: dict[str, Any] = {"flag": False}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("flag == false") is True

    def test_uppercase_still_works(self) -> None:
        ctx: dict[str, Any] = {}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("True") is True
        assert ev.evaluate("False") is False
        assert ev.evaluate_value("None") is None


class TestBinaryOperations:
    def test_rejects_multiplication_before_evaluating_operands(self) -> None:
        ev = SafeExpressionEvaluator({}, {})

        with pytest.raises(ValueError, match="Unsupported binary operator: Mult"):
            ev.evaluate("'a' * 999999999")


class TestUnpacking:
    def test_rejects_keyword_unpacking(self) -> None:
        ev = SafeExpressionEvaluator({"values": {"value": 1}}, {"dict": dict})

        with pytest.raises(ValueError, match="Unsupported keyword unpacking"):
            ev.evaluate_value("dict(**values)")

    def test_rejects_dictionary_unpacking(self) -> None:
        ev = SafeExpressionEvaluator({"values": {"value": 1}}, {})

        with pytest.raises(ValueError, match="Unsupported dictionary unpacking"):
            ev.evaluate_value("{**values}")

    def test_allows_ordinary_keywords_and_dictionary_literals(self) -> None:
        ev = SafeExpressionEvaluator({}, {"dict": dict})

        assert ev.evaluate_value("dict(value=1)") == {"value": 1}
        assert ev.evaluate_value("{'value': 1}") == {"value": 1}


# --- Integration: combined expressions ---


class TestCombinedExpressions:
    def test_boolean_and_with_helpers(self, mock_task_manager: MagicMock) -> None:
        """Test combining task helpers with boolean logic."""
        task = _make_task(closed=True)
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
        """Test .strip() on strings used by workflow conditions."""
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

    def test_string_rpartition_method(self) -> None:
        """Test .rpartition() — used in code-index and context7 rules for file extension matching."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {"path": "src/foo/bar.py"}
        ev = SafeExpressionEvaluator(ctx, {})
        assert ev.evaluate("path.rpartition('.')[2] in ('py', 'ts', 'js')") is True

        ctx2: dict[str, Any] = {"path": "README.md"}
        ev2 = SafeExpressionEvaluator(ctx2, {})
        assert ev2.evaluate("path.rpartition('.')[2] in ('py', 'ts', 'js')") is False

        # No extension
        ctx3: dict[str, Any] = {"path": "Makefile"}
        ev3 = SafeExpressionEvaluator(ctx3, {})
        assert ev3.evaluate("path.rpartition('.')[2] in ('py', 'ts', 'js')") is False

    def test_string_partition_method(self) -> None:
        """Test .partition() — companion to rpartition, both are in SAFE_METHODS."""
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

        ctx: dict[str, Any] = {"path": "src/foo/bar.py"}
        ev = SafeExpressionEvaluator(ctx, {})
        # partition splits on the *first* '.', so the tail is "foo/bar.py"
        # — the extension test below uses the rpartition pattern from rules.
        assert ev.evaluate("path.partition('.')[2]") is True  # truthy non-empty

        ctx2: dict[str, Any] = {"path": "README.md"}
        ev2 = SafeExpressionEvaluator(ctx2, {})
        # First-partition tail is "md" for "README.md"
        assert ev2.evaluate("path.partition('.')[2] == 'md'") is True

        # No extension
        ctx3: dict[str, Any] = {"path": "Makefile"}
        ev3 = SafeExpressionEvaluator(ctx3, {})
        # No '.', so partition returns ('Makefile', '', '') — tail is empty
        assert ev3.evaluate("path.partition('.')[2] == ''") is True

    def test_prompt_filter_expression(self) -> None:
        """Test prompt filtering with length and slash-command checks."""
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
        task = _make_task(closed=True)
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

    @pytest.mark.parametrize(
        "literal",
        [
            "'git  commit'",
            "'before\t\tafter'",
            "'''first\n  second'''",
        ],
    )
    def test_preserves_whitespace_inside_string_literals(self, literal: str) -> None:
        raw = f"value == {literal}"
        assert SafeExpressionEvaluator._normalize_expr(raw) == raw

    def test_evaluate_distinguishes_literal_whitespace(self) -> None:
        evaluator = SafeExpressionEvaluator({"value": "git commit"}, {})
        assert evaluator.evaluate("value == 'git  commit'") is False

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


# ═══════════════════════════════════════════════════════════════════════
# Comprehensions — any/all with generator expressions, list comprehensions
# ═══════════════════════════════════════════════════════════════════════


class TestComprehensions:
    """Test generator expressions and list comprehensions in the safe evaluator."""

    def test_any_with_generator(self) -> None:
        """Test any() with a generator expression — the compress rule pattern."""
        ctx: dict[str, Any] = {"command": "uv run pytest tests/ -v"}
        ev = SafeExpressionEvaluator(ctx, {"any": any, "str": str})
        expr = "any(p in command for p in ['git ', 'pytest', 'ruff '])"
        assert ev.evaluate(expr) is True

    def test_any_with_generator_no_match(self) -> None:
        ctx: dict[str, Any] = {"command": "echo hello"}
        ev = SafeExpressionEvaluator(ctx, {"any": any, "str": str})
        expr = "any(p in command for p in ['git ', 'pytest', 'ruff '])"
        assert ev.evaluate(expr) is False

    def test_all_with_generator(self) -> None:
        ctx: dict[str, Any] = {"items": [2, 4, 6]}
        ev = SafeExpressionEvaluator(ctx, {"all": all})
        assert ev.evaluate("all(x > 0 for x in items)") is True

    def test_all_with_generator_false(self) -> None:
        ctx: dict[str, Any] = {"items": [2, -1, 6]}
        ev = SafeExpressionEvaluator(ctx, {"all": all})
        assert ev.evaluate("all(x > 0 for x in items)") is False

    def test_list_comprehension(self) -> None:
        ctx: dict[str, Any] = {"items": [1, 2, 3]}
        ev = SafeExpressionEvaluator(ctx, {"len": len})
        assert ev.evaluate("len([x for x in items if x > 1])") is True  # 2 > 0

    def test_generator_restores_context(self) -> None:
        """Loop variable doesn't leak into outer context."""
        ctx: dict[str, Any] = {"items": [1, 2], "p": "original"}
        ev = SafeExpressionEvaluator(ctx, {"any": any})
        ev.evaluate("any(p > 1 for p in items)")
        assert ctx["p"] == "original"

    def test_dunder_attribute_access_blocked(self) -> None:
        """Dunder attributes must be rejected to prevent sandbox escape."""
        ctx: dict[str, Any] = {"obj": "hello"}
        ev = SafeExpressionEvaluator(ctx, {})
        with pytest.raises(ValueError, match="dunder attribute"):
            ev.evaluate_value("obj.__class__")

    def test_dunder_chained_access_blocked(self) -> None:
        """Chained dunder traversal like __class__.__base__ must be blocked."""
        ctx: dict[str, Any] = {"obj": []}
        ev = SafeExpressionEvaluator(ctx, {})
        with pytest.raises(ValueError, match="dunder attribute"):
            ev.evaluate_value("obj.__class__.__base__.__subclasses__")


class TestAllowedFuncNameResolution:
    """Names resolve to allowed callables when absent from the context."""

    def test_isinstance_with_dict_type_name(self) -> None:
        ctx: dict[str, Any] = {"output": {"closed": True}}
        ev = SafeExpressionEvaluator(ctx, {"isinstance": isinstance, "dict": dict})
        assert ev.evaluate("isinstance(output, dict)") is True

    def test_isinstance_rejects_list_payload(self) -> None:
        ctx: dict[str, Any] = {"output": [{"type": "text", "text": "not json"}]}
        ev = SafeExpressionEvaluator(ctx, {"isinstance": isinstance, "dict": dict})
        assert ev.evaluate("isinstance(output, dict)") is False

    def test_context_binding_wins_over_allowed_func(self) -> None:
        ctx: dict[str, Any] = {"dict": "shadowed"}
        ev = SafeExpressionEvaluator(ctx, {"dict": dict})
        assert ev.evaluate_value("dict") == "shadowed"

    def test_unknown_name_still_raises(self) -> None:
        ev = SafeExpressionEvaluator({}, {"isinstance": isinstance})
        with pytest.raises(ValueError, match="Unknown variable"):
            ev.evaluate_value("isinstance(missing, dict)")


class TestErrorHandlerConditionTypeSafety:
    """The bundled TASK_CLOSED handler condition tolerates every output shape."""

    CONDITION = "isinstance(tool_output, dict) and tool_output.get('error_code') == \"TASK_CLOSED\""

    @pytest.mark.parametrize(
        ("tool_output", "expected"),
        [
            ({"error_code": "TASK_CLOSED"}, True),
            ({"error_code": "OTHER"}, False),
            ({}, False),
            (None, False),
            ("close_task failed: validation_failed", False),
            ([{"type": "text", "text": "backgrounding notice"}], False),
        ],
    )
    def test_condition_evaluates_without_raising(self, tool_output: Any, expected: bool) -> None:
        ctx: dict[str, Any] = {
            "vars": {},
            "tool_input": {"task_id": "#42"},
            "tool_output": tool_output,
        }
        ev = SafeExpressionEvaluator(ctx, {"isinstance": isinstance, "dict": dict})
        assert ev.evaluate(self.CONDITION) is expected
