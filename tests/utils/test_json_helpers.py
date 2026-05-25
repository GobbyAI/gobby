"""Tests for JSON extraction and decoding utilities."""

from enum import Enum

import msgspec
import pytest

from gobby.utils.json_helpers import (
    decode_llm_response,
    extract_json_from_text,
    extract_json_object,
    json_equal,
)

pytestmark = pytest.mark.unit


class TestExtractJsonFromText:
    """Tests for extract_json_from_text()."""

    def test_extracts_plain_json(self) -> None:
        """Test extraction of plain JSON without wrapping."""
        text = '{"key": "value", "count": 42}'
        result = extract_json_from_text(text)
        assert result == '{"key": "value", "count": 42}'

    def test_extracts_from_markdown_json_block(self) -> None:
        """Test extraction from ```json code block."""
        text = """Here's the response:

```json
{"status": "ok", "data": [1, 2, 3]}
```

That's all!"""
        result = extract_json_from_text(text)
        assert result is not None
        import json

        parsed = json.loads(result)
        assert parsed == {"status": "ok", "data": [1, 2, 3]}

    def test_extracts_from_plain_code_block(self) -> None:
        """Test extraction from plain ``` code block."""
        text = """```
{"result": true}
```"""
        result = extract_json_from_text(text)
        assert result == '{"result": true}'

    def test_handles_nested_backticks_in_strings(self) -> None:
        """Test that backticks inside JSON strings don't break extraction."""
        text = """```json
{
  "description": "Output like:\\n```\\nresult\\n```",
  "count": 1
}
```"""
        result = extract_json_from_text(text)
        assert result is not None
        import json

        parsed = json.loads(result)
        assert "```" in parsed["description"]
        assert parsed["count"] == 1

    def test_handles_braces_in_strings(self) -> None:
        """Test that braces inside strings don't break extraction."""
        text = '{"text": "Hello { world } with {braces}", "nested": {"key": "value"}}'
        result = extract_json_from_text(text)
        assert result is not None
        import json

        parsed = json.loads(result)
        assert parsed["text"] == "Hello { world } with {braces}"
        assert parsed["nested"]["key"] == "value"

    def test_handles_escaped_quotes(self) -> None:
        """Test that escaped quotes in strings are handled."""
        text = r'{"message": "He said \"hello\" to me", "count": 1}'
        result = extract_json_from_text(text)
        assert result is not None
        import json

        parsed = json.loads(result)
        assert 'He said "hello" to me' == parsed["message"]

    def test_returns_none_for_empty_text(self) -> None:
        """Test that empty text returns None."""
        assert extract_json_from_text("") is None
        assert extract_json_from_text(None) is None

    def test_returns_none_for_no_json(self) -> None:
        """Test that text without JSON returns None."""
        assert extract_json_from_text("No JSON here, just text.") is None

    def test_handles_json_with_preamble(self) -> None:
        """Test extraction when JSON has preamble text."""
        text = 'Here\'s your result: {"status": "done"} and that\'s it.'
        result = extract_json_from_text(text)
        assert result == '{"status": "done"}'


class TestExtractJsonObject:
    """Tests for extract_json_object()."""

    def test_extracts_and_parses_object(self) -> None:
        """Test extraction and parsing of JSON object."""
        text = '{"key": "value"}'
        result = extract_json_object(text)
        assert result == {"key": "value"}

    def test_returns_none_for_array(self) -> None:
        """Test that JSON arrays return None (not an object)."""
        text = "[1, 2, 3]"
        result = extract_json_object(text)
        assert result is None

    def test_returns_none_for_no_json(self) -> None:
        """Test that text without JSON returns None."""
        result = extract_json_object("Just text")
        assert result is None


class TestJsonEqual:
    """Tests for json_equal()."""

    def test_compares_json_strings_semantically(self) -> None:
        """Test equivalent JSON strings compare equal regardless of key order."""
        left = '{"status": "ok", "count": 2}'
        right = '{"count": 2, "status": "ok"}'
        assert json_equal(left, right)

    def test_detects_json_string_mismatch(self) -> None:
        """Test different decoded JSON payloads compare unequal."""
        assert not json_equal('{"status": "ok"}', '{"status": "failed"}')

    def test_compares_plain_non_json_strings(self) -> None:
        """Test non-JSON strings fall back to normal equality."""
        assert json_equal("same", "same")
        assert not json_equal("same", "different")


class TaskStatus(str, Enum):
    """Example enum for testing."""

    PENDING = "pending"
    DONE = "done"


class SimpleResult(msgspec.Struct):
    """Simple test struct."""

    status: str
    count: int


class ResultWithEnum(msgspec.Struct):
    """Test struct with enum field."""

    status: TaskStatus
    message: str


class ResultWithOptional(msgspec.Struct):
    """Test struct with optional fields."""

    title: str
    description: str | None = None
    priority: int = 2


class TestDecodeLlmResponse:
    """Tests for decode_llm_response()."""

    def test_decodes_simple_struct(self) -> None:
        """Test basic struct decoding."""
        text = '{"status": "ok", "count": 42}'
        result = decode_llm_response(text, SimpleResult)
        assert result is not None
        assert result.status == "ok"
        assert result.count == 42

    def test_decodes_from_markdown_block(self) -> None:
        """Test decoding from markdown code block."""
        text = """Here's the result:
```json
{"status": "done", "count": 10}
```"""
        result = decode_llm_response(text, SimpleResult)
        assert result is not None
        assert result.status == "done"
        assert result.count == 10

    def test_decodes_enum_values(self) -> None:
        """Test that enum values are properly decoded."""
        text = '{"status": "pending", "message": "Task queued"}'
        result = decode_llm_response(text, ResultWithEnum)
        assert result is not None
        assert result.status == TaskStatus.PENDING
        assert result.message == "Task queued"

    def test_decodes_optional_fields(self) -> None:
        """Test that optional fields work correctly."""
        # With optional field provided
        text = '{"title": "Test", "description": "A test task", "priority": 1}'
        result = decode_llm_response(text, ResultWithOptional)
        assert result is not None
        assert result.title == "Test"
        assert result.description == "A test task"
        assert result.priority == 1

        # With optional field missing (uses default)
        text = '{"title": "Test"}'
        result = decode_llm_response(text, ResultWithOptional)
        assert result is not None
        assert result.title == "Test"
        assert result.description is None
        assert result.priority == 2

    def test_strict_mode_rejects_type_mismatch(self) -> None:
        """Test that strict=True rejects type mismatches."""
        # String "42" should fail for int field in strict mode
        text = '{"status": "ok", "count": "42"}'
        result = decode_llm_response(text, SimpleResult, strict=True)
        assert result is None

    def test_non_strict_mode_coerces_types(self) -> None:
        """Test that strict=False coerces compatible types."""
        # String "42" should coerce to int 42
        text = '{"status": "ok", "count": "42"}'
        result = decode_llm_response(text, SimpleResult, strict=False)
        assert result is not None
        assert result.count == 42

    def test_returns_none_for_missing_required_field(self) -> None:
        """Test that missing required fields return None."""
        text = '{"status": "ok"}'  # missing count
        result = decode_llm_response(text, SimpleResult)
        assert result is None

    def test_returns_none_for_invalid_enum(self) -> None:
        """Test that invalid enum values return None."""
        text = '{"status": "invalid_status", "message": "test"}'
        result = decode_llm_response(text, ResultWithEnum)
        assert result is None

    def test_returns_none_for_empty_text(self) -> None:
        """Test that empty text returns None."""
        result = decode_llm_response("", SimpleResult)
        assert result is None

    def test_returns_none_for_no_json(self) -> None:
        """Test that text without JSON returns None."""
        result = decode_llm_response("No JSON here", SimpleResult)
        assert result is None

    def test_handles_nested_backticks(self) -> None:
        """Test decoding JSON with backticks in string values."""
        text = """```json
{"title": "Add ```code``` support", "description": null}
```"""
        result = decode_llm_response(text, ResultWithOptional)
        assert result is not None
        assert "```" in result.title


class NestedResult(msgspec.Struct):
    """Test struct with nested structure."""

    subtasks: list[SimpleResult]


class TestDecodeLlmResponseNested:
    """Tests for nested struct decoding."""

    def test_decodes_nested_list(self) -> None:
        """Test decoding struct with nested list of structs."""
        text = """{"subtasks": [
            {"status": "done", "count": 1},
            {"status": "pending", "count": 2}
        ]}"""
        result = decode_llm_response(text, NestedResult)
        assert result is not None
        assert len(result.subtasks) == 2
        assert result.subtasks[0].status == "done"
        assert result.subtasks[1].count == 2
