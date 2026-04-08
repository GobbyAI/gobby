"""Tests for gobby.mcp_proxy._coerce_arguments."""

from __future__ import annotations

import pytest

from gobby.mcp_proxy._coerce_arguments import coerce_string_arguments

pytestmark = pytest.mark.unit


class TestCoerceStringArguments:
    """Unit tests for coerce_string_arguments."""

    def test_valid_json_string(self) -> None:
        result = coerce_string_arguments('{"title": "hello", "count": 3}')
        assert result == {"title": "hello", "count": 3}

    def test_escaped_quote_string(self) -> None:
        """The main bug: agents send literal backslash-escaped quotes."""
        result = coerce_string_arguments('{\"title\": \"hello\"}')
        # If Python sees the escapes as literal characters (common from
        # transport layers), the fallback path handles them.
        assert result is not None
        assert result["title"] == "hello"

    def test_escaped_quotes_raw(self) -> None:
        """Raw string with literal backslash-quote — the actual failure case."""
        raw = r'{"title": "hello", "desc": "a \"quoted\" word"}'
        result = coerce_string_arguments(raw)
        # This is actually valid JSON (escaped quotes inside a JSON string
        # value), so json.loads handles it on the fast path.
        assert result is not None
        assert result["title"] == "hello"

    def test_double_escaped_outer(self) -> None:
        r"""The transport-layer pattern: {\"key\": \"value\"}."""
        # Build a string with literal \ before every "
        s = r'{\"title\": \"Test task\", \"priority\": 1}'
        result = coerce_string_arguments(s)
        assert result == {"title": "Test task", "priority": 1}

    def test_nested_single_quotes_in_value(self) -> None:
        r"""Values containing single quotes should parse fine."""
        s = '{\\\"title\\\": \\\"fix \'No messages yet\' bug\\\"}'
        result = coerce_string_arguments(s)
        assert result is not None
        assert "No messages yet" in result["title"]

    def test_empty_object(self) -> None:
        assert coerce_string_arguments("{}") == {}

    def test_garbage_string_returns_none(self) -> None:
        assert coerce_string_arguments("not json at all") is None

    def test_json_array_returns_none(self) -> None:
        """Arrays are valid JSON but not dicts."""
        assert coerce_string_arguments("[1, 2, 3]") is None

    def test_json_scalar_returns_none(self) -> None:
        assert coerce_string_arguments('"just a string"') is None

    def test_empty_string_returns_none(self) -> None:
        assert coerce_string_arguments("") is None

    def test_complex_escaped_payload(self) -> None:
        r"""Realistic agent payload with multiple fields and escaped quotes."""
        # This is the actual pattern agents produce: outer keys escaped,
        # inner arguments value is a string (coerced separately downstream).
        s = (
            r'{\"server_name\": \"gobby-tasks\", \"tool_name\": \"create_task\", '
            r'\"arguments\": \"{\\\"title\\\": \\\"My task\\\"}\"}'
        )
        result = coerce_string_arguments(s)
        assert result is not None
        assert result["server_name"] == "gobby-tasks"
        assert result["tool_name"] == "create_task"
        assert result["arguments"] == '{"title": "My task"}'

    def test_realistic_agent_payload(self) -> None:
        r"""The common case: agent sends flat arguments with escaped quotes."""
        s = r'{\"title\": \"Investigate bug\", \"task_type\": \"bug\", \"priority\": 1, \"category\": \"code\", \"validation_criteria\": \"Tests pass\"}'
        result = coerce_string_arguments(s)
        assert result is not None
        assert result["title"] == "Investigate bug"
        assert result["priority"] == 1
