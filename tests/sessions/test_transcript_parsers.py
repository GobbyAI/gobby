"""
Tests for Transcript Parsers (Claude, Codex, Gemini, Cursor).
Consolidated from individual files.
"""

import json
from datetime import UTC, datetime

import pytest

from gobby.sessions.transcripts import PARSER_REGISTRY, get_parser
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser

pytestmark = pytest.mark.unit


class TestParsedMessage:
    """Tests for ParsedMessage dataclass."""

    def test_model_field_defaults_to_none(self) -> None:
        """Test that ParsedMessage model field defaults to None."""
        msg = ParsedMessage(
            index=0,
            role="assistant",
            content="Hello",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(UTC),
            raw_json={},
        )
        assert msg.model is None

    def test_model_field_accepts_value(self) -> None:
        """Test that ParsedMessage model field can be set."""
        msg = ParsedMessage(
            index=0,
            role="assistant",
            content="Hello",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(UTC),
            raw_json={},
            model="claude-opus-4-5-20251101",
        )
        assert msg.model == "claude-opus-4-5-20251101"


class TestClaudeTranscriptParser:
    """Tests for Claude transcript parser."""

    @pytest.fixture
    def parser(self):
        return ClaudeTranscriptParser()

    def test_extract_usage_returns_tuple_with_model(self, parser) -> None:
        """Test that _extract_usage returns tuple of (TokenUsage | None, str | None)."""
        data = {
            "type": "agent",
            "message": {
                "model": "claude-opus-4-5-20251101",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            },
        }
        usage, model = parser._extract_usage(data)
        assert usage is not None
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert model == "claude-opus-4-5-20251101"

    def test_extract_usage_returns_none_model_when_missing(self, parser) -> None:
        """Test that _extract_usage returns None model when not present."""
        data = {
            "type": "agent",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            },
        }
        usage, model = parser._extract_usage(data)
        assert usage is not None
        assert model is None

    def test_extract_usage_returns_none_tuple_when_no_usage(self, parser) -> None:
        """Test that _extract_usage returns (None, model) when no usage data."""
        data = {
            "type": "agent",
            "message": {
                "model": "claude-opus-4-5-20251101",
                "content": "Hello",
            },
        }
        usage, model = parser._extract_usage(data)
        assert usage is None
        assert model == "claude-opus-4-5-20251101"

    def test_parse_line_extracts_model(self, parser) -> None:
        """Test that parse_line sets model on ParsedMessage."""
        line = json.dumps(
            {
                "type": "agent",
                "message": {
                    "model": "claude-opus-4-5-20251101",
                    "content": [{"type": "text", "text": "Hello"}],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                    },
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.model == "claude-opus-4-5-20251101"

    def test_parse_line_user(self, parser) -> None:
        line = json.dumps(
            {
                "type": "user",
                "message": {"content": "Hello world"},
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

        msg = parser.parse_line(line, 0)

        assert msg is not None
        assert msg.role == "user"
        assert msg.content == "Hello world"
        assert msg.content_type == "text"
        assert msg.index == 0

    def test_parse_line_assistant_text_blocks(self, parser) -> None:
        line = json.dumps(
            {
                "type": "agent",
                "message": {
                    "content": [
                        {"type": "text", "text": "Part 1"},
                        {"type": "text", "text": "Part 2"},
                    ]
                },
                "timestamp": "2024-01-01T12:00:01Z",
            }
        )

        msg = parser.parse_line(line, 1)

        assert msg is not None
        assert msg.role == "assistant"
        # Parser joins with space
        assert msg.content == "Part 1 Part 2"

    def test_parse_line_tool_use(self, parser) -> None:
        line = json.dumps(
            {
                "type": "agent",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "read_file", "input": {"path": "foo.txt"}}
                    ]
                },
                "timestamp": "2024-01-01T12:00:02Z",
            }
        )

        msg = parser.parse_line(line, 2)

        assert msg is not None
        assert msg.role == "assistant"
        assert msg.content_type == "tool_use"
        assert msg.tool_name == "read_file"
        assert msg.tool_input == {"path": "foo.txt"}

    def test_parse_line_tool_result(self, parser) -> None:
        line = json.dumps(
            {
                "type": "tool_result",
                "tool_name": "read_file",
                "result": "file content",
                "timestamp": "2024-01-01T12:00:03Z",
            }
        )

        msg = parser.parse_line(line, 3)

        assert msg is not None
        assert msg.role == "tool"
        assert msg.content_type == "tool_result"
        assert msg.tool_name == "read_file"
        assert msg.content == "file content"

    def test_parse_line_invalid_json(self, parser) -> None:
        # Should handle gracefully and log warning
        msg = parser.parse_line("invalid json", 0)
        assert msg is None

    def test_parse_line_unknown_type(self, parser) -> None:
        line = json.dumps({"type": "unknown_event"})
        msg = parser.parse_line(line, 0)
        assert msg is None

    def test_parse_lines_continuous(self, parser) -> None:
        lines = [
            json.dumps({"type": "user", "message": {"content": "Hi"}}),
            json.dumps(
                {"type": "agent", "message": {"content": [{"type": "text", "text": "Hello"}]}}
            ),
        ]

        msgs = parser.parse_lines(lines, start_index=10)

        assert len(msgs) == 2
        assert msgs[0].index == 10
        assert msgs[0].role == "user"
        assert msgs[1].index == 11
        assert msgs[1].role == "assistant"

    def test_is_session_boundary(self, parser) -> None:
        # Standard user message
        assert not parser.is_session_boundary({"type": "user", "message": {"content": "hello"}})

        # Clear command
        assert parser.is_session_boundary(
            {
                "type": "user",
                "message": {"content": "blah <command-name>/clear</command-name> blah"},
            }
        )

        # Agent message (never a boundary)
        assert not parser.is_session_boundary(
            {"type": "agent", "message": {"content": "cleaning up..."}}
        )

    def test_extract_last_messages(self, parser) -> None:
        turns = [
            {"message": {"role": "user", "content": "1"}},
            {"message": {"role": "assistant", "content": "2"}},
            {"message": {"role": "user", "content": "3"}},
            {"message": {"role": "assistant", "content": "4"}},
        ]

        # helper to mock turn format
        msgs = parser.extract_last_messages(turns, num_pairs=1)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "3"
        assert msgs[1]["content"] == "4"

        msgs = parser.extract_last_messages(turns, num_pairs=2)
        assert len(msgs) == 4
        assert msgs[0]["content"] == "1"

    def test_extract_last_messages_complex_content(self, parser) -> None:
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Part 1"},
                        {"type": "text", "text": "Part 2"},
                    ],
                }
            }
        ]
        msgs = parser.extract_last_messages(turns, 1)
        assert msgs[0]["content"] == "Part 1 Part 2"

    def test_extract_turns_since_clear_no_clear(self, parser) -> None:
        turns = [{"type": "user"}] * 10
        extracted = parser.extract_turns_since_clear(turns, max_turns=5)
        assert len(extracted) == 5

    def test_extract_turns_since_clear_no_cap(self, parser) -> None:
        """Default (no max_turns) returns all turns without truncation."""
        turns = [{"type": "user"}] * 200
        extracted = parser.extract_turns_since_clear(turns)
        assert len(extracted) == 200

    def test_extract_turns_since_clear_with_boundary(self, parser) -> None:
        turns = [
            {"type": "user", "message": {"content": "before"}},
            {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
            {"type": "user", "message": {"content": "after1"}},
            {"type": "agent", "message": {"content": "after2"}},
        ]

        extracted = parser.extract_turns_since_clear(turns)
        assert len(extracted) == 2
        assert extracted[0]["message"]["content"] == "after1"

    def test_extract_turns_since_clear_consecutive(self, parser) -> None:
        turns = [
            {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
            {
                "type": "user",
                "message": {"content": "<command-name>/clear</command-name>"},
            },  # consecutive
            {"type": "user", "message": {"content": "real start"}},
        ]
        extracted = parser.extract_turns_since_clear(turns)
        assert len(extracted) == 1
        assert extracted[0]["message"]["content"] == "real start"

    def test_parse_line_tool_use_extracts_id(self, parser) -> None:
        """Test that tool_use_id is extracted from tool_use blocks."""
        line = json.dumps(
            {
                "type": "agent",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_abc123",
                            "name": "read_file",
                            "input": {"path": "foo.txt"},
                        }
                    ]
                },
                "timestamp": "2024-01-01T12:00:02Z",
            }
        )

        msg = parser.parse_line(line, 2)

        assert msg is not None
        assert msg.tool_use_id == "toolu_abc123"

    def test_parse_line_tool_result_extracts_id(self, parser) -> None:
        """Test that tool_use_id is extracted from tool_result messages."""
        line = json.dumps(
            {
                "type": "tool_result",
                "tool_name": "read_file",
                "tool_use_id": "toolu_abc123",
                "result": "file content",
                "timestamp": "2024-01-01T12:00:03Z",
            }
        )

        msg = parser.parse_line(line, 3)

        assert msg is not None
        assert msg.tool_use_id == "toolu_abc123"

    def test_validate_tool_pairing_empty(self, parser) -> None:
        """Test _validate_tool_pairing with empty turns."""
        cleaned, removed = parser._validate_tool_pairing([])
        assert cleaned == []
        assert removed == []

    def test_validate_tool_pairing_properly_paired(self, parser) -> None:
        """Test _validate_tool_pairing with properly paired tool_use/tool_result."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_001", "name": "read"},
                    ],
                }
            },
            {
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_001", "content": "ok"},
                    ],
                }
            },
        ]

        cleaned, removed = parser._validate_tool_pairing(turns)

        assert len(cleaned) == 2
        assert removed == []
        # Content should be unchanged
        assert cleaned[1]["message"]["content"][0]["tool_use_id"] == "toolu_001"

    def test_validate_tool_pairing_orphaned_result(self, parser) -> None:
        """Test _validate_tool_pairing removes orphaned tool_result."""
        turns = [
            # No tool_use, just an orphaned tool_result
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_orphan",
                            "content": "orphaned",
                        },
                    ],
                }
            },
        ]

        cleaned, removed = parser._validate_tool_pairing(turns)

        assert len(cleaned) == 1
        assert removed == ["toolu_orphan"]
        # The tool_result block should be removed
        assert cleaned[0]["message"]["content"] == []

    def test_validate_tool_pairing_mixed(self, parser) -> None:
        """Test _validate_tool_pairing with mixed valid and orphaned results."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_valid", "name": "read"},
                    ],
                }
            },
            {
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_valid", "content": "ok"},
                        {"type": "tool_result", "tool_use_id": "toolu_orphan", "content": "bad"},
                    ],
                }
            },
        ]

        cleaned, removed = parser._validate_tool_pairing(turns)

        assert removed == ["toolu_orphan"]
        # Valid result should remain
        assert len(cleaned[1]["message"]["content"]) == 1
        assert cleaned[1]["message"]["content"][0]["tool_use_id"] == "toolu_valid"

    def test_validate_tool_pairing_multiple_tool_use(self, parser) -> None:
        """Test _validate_tool_pairing with multiple tool_use in one message."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_001", "name": "read"},
                        {"type": "tool_use", "id": "toolu_002", "name": "write"},
                    ],
                }
            },
            {
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_001", "content": "ok1"},
                        {"type": "tool_result", "tool_use_id": "toolu_002", "content": "ok2"},
                    ],
                }
            },
        ]

        cleaned, removed = parser._validate_tool_pairing(turns)

        assert removed == []
        assert len(cleaned[1]["message"]["content"]) == 2

    def test_extract_turns_since_clear_validates_tool_pairing(self, parser) -> None:
        """Test that extract_turns_since_clear removes orphaned tool_results after truncation."""
        # Create turns where truncation would orphan a tool_result
        turns = []
        # Add a tool_use that will be truncated away
        turns.append(
            {
                "type": "agent",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_truncated", "name": "read"}],
                },
            }
        )
        # Add many user messages to push the tool_use out of range
        for i in range(60):
            turns.append({"type": "user", "message": {"content": f"msg {i}"}})
        # Add a tool_result referencing the truncated tool_use (edge case)
        turns.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_truncated",
                            "content": "late result",
                        },
                    ],
                },
            }
        )

        # Extract with max_turns=50, which should truncate the tool_use
        extracted = parser.extract_turns_since_clear(turns, max_turns=50)

        # The orphaned tool_result should be removed from the last turn
        last_turn = extracted[-1]
        content = last_turn["message"]["content"]
        # Either content is empty list or the tool_result block was removed
        has_orphan = any(
            isinstance(b, dict) and b.get("tool_use_id") == "toolu_truncated"
            for b in (content if isinstance(content, list) else [])
        )
        assert not has_orphan, "Orphaned tool_result should have been removed"


class TestClaudeExpandLine:
    """Tests for _expand_line multi-block expansion."""

    @pytest.fixture
    def parser(self):
        return ClaudeTranscriptParser()

    def test_expand_user_string_content(self, parser) -> None:
        """User message with string content produces one text message."""
        line = json.dumps(
            {"type": "user", "message": {"content": "Hello"}, "timestamp": "2024-01-01T12:00:00Z"}
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello"
        assert msgs[0].content_type == "text"

    def test_expand_user_tool_result_blocks(self, parser) -> None:
        """User message with tool_result blocks produces tool_result messages."""
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_001",
                            "content": "file contents here",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_002",
                            "content": [{"type": "text", "text": "second result"}],
                            "is_error": True,
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 2

        assert msgs[0].role == "user"
        assert msgs[0].content_type == "tool_result"
        assert msgs[0].content == "file contents here"
        assert msgs[0].tool_use_id == "toolu_001"
        assert msgs[0].tool_result == {"content": "file contents here", "is_error": False}

        assert msgs[1].role == "user"
        assert msgs[1].content_type == "tool_result"
        assert msgs[1].content == "second result"
        assert msgs[1].tool_use_id == "toolu_002"
        assert msgs[1].tool_result["is_error"] is True

    def test_expand_user_mixed_text_and_tool_result(self, parser) -> None:
        """User message with text + tool_result blocks separates them."""
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "Here is the result:"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "output data",
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 2
        assert msgs[0].content_type == "text"
        assert msgs[0].content == "Here is the result:"
        assert msgs[1].content_type == "tool_result"
        assert msgs[1].content == "output data"

    def test_expand_assistant_text_and_tool_use(self, parser) -> None:
        """Assistant message with text + tool_use blocks expands into separate messages."""
        line = json.dumps(
            {
                "type": "agent",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me read that file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_read1",
                            "name": "Read",
                            "input": {"file_path": "/tmp/test.txt"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_read2",
                            "name": "Grep",
                            "input": {"pattern": "foo"},
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 3

        assert msgs[0].role == "assistant"
        assert msgs[0].content_type == "text"
        assert msgs[0].content == "Let me read that file."

        assert msgs[1].content_type == "tool_use"
        assert msgs[1].tool_name == "Read"
        assert msgs[1].tool_input == {"file_path": "/tmp/test.txt"}
        assert msgs[1].tool_use_id == "toolu_read1"

        assert msgs[2].content_type == "tool_use"
        assert msgs[2].tool_name == "Grep"
        assert msgs[2].tool_use_id == "toolu_read2"

    def test_expand_assistant_with_thinking(self, parser) -> None:
        """Assistant message with thinking blocks produces a thinking message."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "Let me think about this..."},
                        {"type": "text", "text": "Here's my answer."},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 2

        # Text comes first in output order (text block before thinking in results)
        text_msgs = [m for m in msgs if m.content_type == "text"]
        thinking_msgs = [m for m in msgs if m.content_type == "thinking"]
        assert len(text_msgs) == 1
        assert len(thinking_msgs) == 1
        assert text_msgs[0].content == "Here's my answer."
        assert thinking_msgs[0].content == "Let me think about this..."

    def test_expand_assistant_empty_thinking(self, parser) -> None:
        """Thinking block with empty string produces no thinking message."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": ""},
                        {"type": "text", "text": "Answer."},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        thinking_msgs = [m for m in msgs if m.content_type == "thinking"]
        assert len(thinking_msgs) == 0
        text_msgs = [m for m in msgs if m.content_type == "text"]
        assert len(text_msgs) == 1

    def test_expand_assistant_null_thinking(self, parser) -> None:
        """Thinking block with null value produces no thinking message and no TypeError."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": None},
                        {"type": "text", "text": "Answer."},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        thinking_msgs = [m for m in msgs if m.content_type == "thinking"]
        assert len(thinking_msgs) == 0

    def test_expand_assistant_missing_thinking_field(self, parser) -> None:
        """Thinking block with no 'thinking' field produces no thinking message."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking"},
                        {"type": "text", "text": "Answer."},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        thinking_msgs = [m for m in msgs if m.content_type == "thinking"]
        assert len(thinking_msgs) == 0

    def test_expand_assistant_whitespace_thinking(self, parser) -> None:
        """Thinking block with only whitespace produces no thinking message."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "  \n  "},
                        {"type": "text", "text": "Answer."},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        thinking_msgs = [m for m in msgs if m.content_type == "thinking"]
        assert len(thinking_msgs) == 0

    def test_expand_assistant_valid_and_empty_thinking(self, parser) -> None:
        """Mixed valid and empty thinking blocks — only valid one emitted."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": ""},
                        {"type": "thinking", "thinking": "Real thought."},
                        {"type": "thinking", "thinking": None},
                        {"type": "text", "text": "Answer."},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        thinking_msgs = [m for m in msgs if m.content_type == "thinking"]
        assert len(thinking_msgs) == 1
        assert thinking_msgs[0].content == "Real thought."

    def test_expand_top_level_tool_result(self, parser) -> None:
        """Top-level tool_result type produces one tool_result message."""
        line = json.dumps(
            {
                "type": "tool_result",
                "tool_name": "Read",
                "tool_use_id": "toolu_xyz",
                "result": "file content here",
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 1
        assert msgs[0].role == "tool"
        assert msgs[0].content_type == "tool_result"
        assert msgs[0].tool_name == "Read"
        assert msgs[0].tool_use_id == "toolu_xyz"

    def test_expand_unknown_type_returns_empty(self, parser) -> None:
        """Unknown message type returns empty list."""
        line = json.dumps({"type": "progress", "timestamp": "2024-01-01T12:00:00Z"})
        msgs = parser._expand_line(line, 0)
        assert msgs == []

    def test_expand_invalid_json_returns_empty(self, parser) -> None:
        """Invalid JSON returns empty list."""
        msgs = parser._expand_line("not json", 0)
        assert msgs == []

    def test_parse_lines_expansion_sequential_indices(self, parser) -> None:
        """parse_lines assigns sequential indices across expanded messages."""
        lines = [
            # User prompt
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "Read two files"},
                    "timestamp": "2024-01-01T12:00:00Z",
                }
            ),
            # Assistant with text + 2 tool_use blocks → 3 messages
            json.dumps(
                {
                    "type": "agent",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Sure."},
                            {
                                "type": "tool_use",
                                "id": "toolu_a",
                                "name": "Read",
                                "input": {"file_path": "a.txt"},
                            },
                            {
                                "type": "tool_use",
                                "id": "toolu_b",
                                "name": "Read",
                                "input": {"file_path": "b.txt"},
                            },
                        ]
                    },
                    "timestamp": "2024-01-01T12:00:01Z",
                }
            ),
            # User with tool results
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_a",
                                "content": "contents of a",
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_b",
                                "content": "contents of b",
                            },
                        ]
                    },
                    "timestamp": "2024-01-01T12:00:02Z",
                }
            ),
        ]
        msgs = parser.parse_lines(lines, start_index=0)

        # 1 user + 3 assistant (text + 2 tool_use) + 2 tool_result = 6
        assert len(msgs) == 6
        assert [m.index for m in msgs] == [0, 1, 2, 3, 4, 5]
        assert msgs[0].content == "Read two files"
        assert msgs[1].content == "Sure."
        assert msgs[2].content_type == "tool_use"
        assert msgs[2].tool_name == "Read"
        assert msgs[3].content_type == "tool_use"
        assert msgs[4].content_type == "tool_result"
        assert msgs[4].tool_use_id == "toolu_a"
        assert msgs[5].content_type == "tool_result"
        assert msgs[5].tool_use_id == "toolu_b"

    def test_parse_line_backward_compat_user_list_tool_results(self, parser) -> None:
        """parse_line with user list content containing only tool_results returns tool_result."""
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_001",
                            "content": "result text",
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content_type == "tool_result"
        assert msg.content == "result text"
        assert msg.tool_use_id == "toolu_001"

    def test_parse_line_backward_compat_user_list_with_text(self, parser) -> None:
        """parse_line with user list content extracts text when available."""
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "user prompt"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_001",
                            "content": "ignored in single mode",
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content_type == "text"
        assert msg.content == "user prompt"


class TestCodexTranscriptParser:
    """Tests for Codex transcript parser (envelope format)."""

    @pytest.fixture
    def parser(self):
        return CodexTranscriptParser()

    # -- Helpers --

    @staticmethod
    def _msg(role: str, text: str, ts: str = "2024-06-15T10:30:00Z", **extra) -> str:
        """Build a Codex response_item/message envelope line."""
        block_type = "output_text" if role == "assistant" else "input_text"
        payload: dict = {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
            **extra,
        }
        return json.dumps({"timestamp": ts, "type": "response_item", "payload": payload})

    @staticmethod
    def _function_call(
        name: str, arguments: str, call_id: str, ts: str = "2024-06-15T10:30:00Z"
    ) -> str:
        """Build a Codex response_item/function_call envelope line."""
        return json.dumps(
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": name,
                    "arguments": arguments,
                    "call_id": call_id,
                },
            }
        )

    @staticmethod
    def _function_call_output(call_id: str, output: str, ts: str = "2024-06-15T10:30:00Z") -> str:
        """Build a Codex response_item/function_call_output envelope line."""
        return json.dumps(
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )

    @staticmethod
    def _event_msg(event_type: str, ts: str = "2024-06-15T10:30:00Z", **extra) -> str:
        """Build a Codex event_msg envelope line."""
        return json.dumps(
            {"timestamp": ts, "type": "event_msg", "payload": {"type": event_type, **extra}}
        )

    @staticmethod
    def _session_meta(session_id: str = "abc-123", ts: str = "2024-06-15T10:30:00Z") -> str:
        return json.dumps(
            {"timestamp": ts, "type": "session_meta", "payload": {"id": session_id}}
        )

    @staticmethod
    def _turn_context(turn_id: str = "turn-1", ts: str = "2024-06-15T10:30:00Z") -> str:
        return json.dumps(
            {"timestamp": ts, "type": "turn_context", "payload": {"turn_id": turn_id}}
        )

    # -- parse_line: messages --

    def test_parse_user_message(self, parser) -> None:
        line = self._msg("user", "hello world")
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "user"
        assert msg.content == "hello world"
        assert msg.content_type == "text"
        assert msg.index == 0

    def test_parse_assistant_message(self, parser) -> None:
        line = self._msg("assistant", "ok", phase="final_answer")
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "assistant"
        assert msg.content == "ok"

    def test_parse_developer_maps_to_system(self, parser) -> None:
        line = self._msg("developer", "System prompt")
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "system"
        assert msg.content == "System prompt"

    def test_parse_multiple_content_blocks(self, parser) -> None:
        payload = {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "First part."},
                {"type": "output_text", "text": "Second part."},
            ],
        }
        line = json.dumps(
            {"timestamp": "2024-06-15T10:30:00Z", "type": "response_item", "payload": payload}
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content == "First part. Second part."

    def test_parse_empty_content_blocks(self, parser) -> None:
        payload = {"type": "message", "role": "user", "content": []}
        line = json.dumps(
            {"timestamp": "2024-06-15T10:30:00Z", "type": "response_item", "payload": payload}
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content == ""

    def test_parse_message_missing_role(self, parser) -> None:
        payload = {"type": "message", "content": [{"type": "input_text", "text": "hi"}]}
        line = json.dumps(
            {"timestamp": "2024-06-15T10:30:00Z", "type": "response_item", "payload": payload}
        )
        msg = parser.parse_line(line, 0)
        assert msg is None

    # -- parse_line: function calls --

    def test_parse_function_call(self, parser) -> None:
        line = self._function_call(
            "exec_command", '{"cmd":"ls","workdir":"/tmp"}', "call_abc"
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "assistant"
        assert msg.content_type == "tool_use"
        assert msg.tool_name == "exec_command"
        assert msg.tool_input == {"cmd": "ls", "workdir": "/tmp"}
        assert msg.tool_use_id == "call_abc"

    def test_parse_function_call_invalid_arguments(self, parser) -> None:
        line = self._function_call("broken", "not json", "call_bad")
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.tool_input == {"raw": "not json"}

    def test_parse_function_call_output(self, parser) -> None:
        line = self._function_call_output("call_abc", "file1.txt\nfile2.txt")
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "tool"
        assert msg.content_type == "tool_result"
        assert msg.tool_use_id == "call_abc"
        assert msg.tool_result == {"output": "file1.txt\nfile2.txt"}
        assert "file1.txt" in msg.content

    # -- parse_line: skipped types --

    def test_skip_event_msg(self, parser) -> None:
        line = self._event_msg("task_started", turn_id="turn-1")
        assert parser.parse_line(line, 0) is None

    def test_skip_session_meta(self, parser) -> None:
        assert parser.parse_line(self._session_meta(), 0) is None

    def test_skip_turn_context(self, parser) -> None:
        assert parser.parse_line(self._turn_context(), 0) is None

    # -- parse_line: error handling --

    def test_parse_line_invalid_json(self, parser) -> None:
        assert parser.parse_line("not valid json", 0) is None

    def test_parse_line_empty(self, parser) -> None:
        assert parser.parse_line("", 0) is None
        assert parser.parse_line("   ", 0) is None

    def test_parse_line_non_dict_json(self, parser) -> None:
        assert parser.parse_line('"just a string"', 0) is None
        assert parser.parse_line("42", 0) is None

    def test_parse_line_missing_payload(self, parser) -> None:
        line = json.dumps({"timestamp": "2024-06-15T10:30:00Z", "type": "response_item"})
        assert parser.parse_line(line, 0) is None

    # -- parse_lines batch --

    def test_parse_lines(self, parser) -> None:
        lines = [
            self._session_meta(),
            self._event_msg("task_started"),
            self._msg("user", "First"),
            "",
            self._msg("assistant", "Second"),
            "invalid json",
            self._function_call("exec_command", '{"cmd":"ls"}', "call_1"),
            self._function_call_output("call_1", "output"),
            self._msg("user", "Third"),
        ]

        msgs = parser.parse_lines(lines, start_index=5)

        assert len(msgs) == 5
        assert msgs[0].index == 5
        assert msgs[0].content == "First"
        assert msgs[0].role == "user"
        assert msgs[1].index == 6
        assert msgs[1].content == "Second"
        assert msgs[2].index == 7
        assert msgs[2].content_type == "tool_use"
        assert msgs[3].index == 8
        assert msgs[3].content_type == "tool_result"
        assert msgs[4].index == 9
        assert msgs[4].content == "Third"

    # -- extract_last_messages --

    def test_extract_last_messages(self, parser) -> None:
        turns = [
            json.loads(self._msg("user", "1")),
            json.loads(self._msg("assistant", "2")),
            json.loads(self._event_msg("token_count")),
            json.loads(self._msg("user", "3")),
            json.loads(self._msg("assistant", "4")),
            json.loads(self._msg("user", "5")),
            json.loads(self._msg("assistant", "6")),
        ]

        msgs = parser.extract_last_messages(turns, num_pairs=1)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "5"
        assert msgs[1]["content"] == "6"

        msgs = parser.extract_last_messages(turns, num_pairs=2)
        assert len(msgs) == 4
        assert msgs[0]["content"] == "3"

        msgs = parser.extract_last_messages(turns, num_pairs=10)
        assert len(msgs) == 6

    def test_extract_last_messages_skips_non_messages(self, parser) -> None:
        turns = [
            json.loads(self._function_call("exec_command", '{"cmd":"ls"}', "c1")),
            json.loads(self._msg("user", "hello")),
            json.loads(self._msg("assistant", "hi")),
        ]
        msgs = parser.extract_last_messages(turns, num_pairs=5)
        assert len(msgs) == 2

    def test_extract_last_messages_developer_mapped(self, parser) -> None:
        turns = [
            json.loads(self._msg("developer", "System instructions")),
            json.loads(self._msg("user", "hello")),
        ]
        msgs = parser.extract_last_messages(turns, num_pairs=5)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"

    def test_extract_last_messages_empty(self, parser) -> None:
        assert parser.extract_last_messages([], num_pairs=2) == []

    # -- extract_turns_since_clear --

    def test_extract_turns_since_clear(self, parser) -> None:
        turns = [json.loads(self._msg("user", str(i))) for i in range(100)]

        extracted = parser.extract_turns_since_clear(turns, max_turns=50)
        assert len(extracted) == 50

        small_turns = [json.loads(self._msg("user", str(i))) for i in range(10)]
        extracted = parser.extract_turns_since_clear(small_turns, max_turns=50)
        assert len(extracted) == 10

        extracted = parser.extract_turns_since_clear(turns)
        assert len(extracted) == 100

    def test_extract_turns_since_clear_respects_session_boundary(self, parser) -> None:
        turns = [
            json.loads(self._msg("user", "old")),
            json.loads(self._msg("assistant", "old reply")),
            json.loads(self._session_meta()),
            json.loads(self._msg("user", "new")),
            json.loads(self._msg("assistant", "new reply")),
        ]
        extracted = parser.extract_turns_since_clear(turns)
        assert len(extracted) == 2

    # -- is_session_boundary --

    def test_is_session_boundary(self, parser) -> None:
        assert parser.is_session_boundary(json.loads(self._session_meta())) is True
        assert parser.is_session_boundary(json.loads(self._event_msg("task_started"))) is False
        assert parser.is_session_boundary(json.loads(self._msg("user", "hi"))) is False
        assert parser.is_session_boundary({}) is False

    # -- timestamp --

    def test_timestamp_parsing(self, parser) -> None:
        line = self._msg("user", "Hello", ts="2024-06-15T10:30:00Z")
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.timestamp.year == 2024
        assert msg.timestamp.month == 6
        assert msg.timestamp.day == 15

    def test_timestamp_missing(self, parser) -> None:
        line = json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.timestamp is not None

    def test_timestamp_invalid_format(self, parser) -> None:
        payload = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }
        line = json.dumps(
            {"timestamp": "not-a-date", "type": "response_item", "payload": payload}
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.timestamp is not None

    # -- usage --

    def test_usage_returns_none_for_messages(self, parser) -> None:
        """Usage data lives on event_msg lines, not on message lines."""
        line = self._msg("assistant", "Response")
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.usage is None


class TestGeminiTranscriptParser:
    """Tests for Gemini transcript parser."""

    @pytest.fixture
    def parser(self):
        return GeminiTranscriptParser()

    def test_gemini_parser_generic_message(self, parser) -> None:
        # Test simple user message via type-based format
        line = json.dumps(
            {
                "type": "message",
                "role": "user",
                "content": "Hello world",
                "timestamp": "2023-01-01T12:00:00Z",
            }
        )

        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "user"
        assert msg.content == "Hello world"
        assert msg.index == 0
        assert msg.timestamp.year == 2023

    def test_gemini_parser_model_response(self, parser) -> None:
        # Test model response via type-based format
        line = json.dumps(
            {
                "type": "message",
                "role": "model",
                "content": "I am Gemini",
                "timestamp": "2023-01-01T12:00:01Z",
            }
        )

        msg = parser.parse_line(line, 1)
        assert msg is not None
        assert msg.role == "assistant"  # Normalized
        assert msg.content == "I am Gemini"

    def test_gemini_parser_nested_message_structure_skipped(self, parser) -> None:
        # Legacy nested message structure without type field is now skipped
        line = json.dumps(
            {
                "message": {"role": "user", "content": "Nested content"},
                "timestamp": "2023-01-01T12:00:02Z",
            }
        )

        msg = parser.parse_line(line, 2)
        assert msg is None

    def test_gemini_parser_list_content(self, parser) -> None:
        # Test content as list of parts via type-based format
        line = json.dumps({"type": "model", "content": [{"text": "Part 1"}, "Part 2"]})

        msg = parser.parse_line(line, 3)
        assert msg is not None
        assert "Part 1" in msg.content
        assert "Part 2" in msg.content

    def test_gemini_parse_line_empty(self, parser) -> None:
        """Test handling of empty/whitespace lines."""
        assert parser.parse_line("", 0) is None
        assert parser.parse_line("   ", 0) is None

    def test_gemini_parse_line_invalid_json(self, parser) -> None:
        """Test handling of invalid JSON."""
        msg = parser.parse_line("not valid json", 0)
        assert msg is None

    def test_gemini_parse_line_unknown_type(self, parser) -> None:
        """Test handling of messages without role."""
        line = json.dumps({"data": "something"})
        msg = parser.parse_line(line, 0)
        assert msg is None

    def test_gemini_parse_line_type_field_user(self, parser) -> None:
        """Test parsing with type field as 'user'."""
        line = json.dumps({"type": "user", "content": "From type field"})
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "user"

    def test_gemini_parse_line_type_field_model(self, parser) -> None:
        """Test parsing with type field as 'model'."""
        line = json.dumps({"type": "model", "content": "Model response"})
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "assistant"

    def test_gemini_parse_line_tool_result(self, parser) -> None:
        """Test parsing tool_result event."""
        line = json.dumps(
            {
                "type": "tool_result",
                "tool_name": "read_file",
                "output": "some result",
                "status": "success",
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.role == "tool"
        assert msg.content_type == "tool_result"
        assert "some result" in msg.content

    def test_gemini_parse_line_function_call(self, parser) -> None:
        """Test parsing functionCall in content via type-based format."""
        line = json.dumps(
            {
                "type": "model",
                "content": [
                    {"text": "Let me call a function"},
                    {"functionCall": {"name": "read_file", "args": {"path": "test.txt"}}},
                ],
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content_type == "tool_use"
        assert msg.tool_name == "read_file"
        assert msg.tool_input == {"path": "test.txt"}
        assert "Let me call a function" in msg.content

    def test_gemini_extract_last_messages(self, parser) -> None:
        """Test extract_last_messages with type-based format."""
        turns = [
            {"type": "message", "role": "user", "content": "1"},
            {"type": "message", "role": "model", "content": "2"},
            {"type": "message", "role": "user", "content": "3"},
            {"type": "message", "role": "model", "content": "4"},
        ]

        msgs = parser.extract_last_messages(turns, num_pairs=1)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "3"
        assert msgs[0]["role"] == "user"
        assert msgs[1]["content"] == "4"
        assert msgs[1]["role"] == "assistant"  # Normalized from model

    def test_gemini_extract_last_messages_list_content(self, parser) -> None:
        """Test extract_last_messages with list content."""
        turns = [
            {"type": "message", "role": "user", "content": ["part1", "part2"]},
            {"type": "message", "role": "model", "content": [{"text": "response"}]},
        ]

        msgs = parser.extract_last_messages(turns, num_pairs=1)
        assert len(msgs) == 2
        assert "part1" in msgs[0]["content"]
        assert "part2" in msgs[0]["content"]

    def test_gemini_extract_last_messages_nested_message_skipped(self, parser) -> None:
        """Test extract_last_messages skips legacy nested message structure."""
        turns = [
            {"message": {"role": "user", "content": "nested user"}},
            {"message": {"role": "assistant", "content": "nested assistant"}},
        ]

        msgs = parser.extract_last_messages(turns, num_pairs=1)
        assert len(msgs) == 0  # Legacy format is now skipped

    def test_gemini_extract_turns_since_clear(self, parser) -> None:
        """Test extract_turns_since_clear."""
        turns = [{"role": "user"}] * 100

        extracted = parser.extract_turns_since_clear(turns, max_turns=50)
        assert len(extracted) == 50

        small_turns = [{"role": "user"}] * 10
        extracted = parser.extract_turns_since_clear(small_turns, max_turns=50)
        assert len(extracted) == 10

    def test_gemini_is_session_boundary(self, parser) -> None:
        """Test is_session_boundary always returns False for Gemini."""
        assert parser.is_session_boundary({}) is False
        assert parser.is_session_boundary({"role": "user"}) is False

    def test_gemini_parse_lines(self, parser) -> None:
        """Test batch parsing with parse_lines."""
        lines = [
            json.dumps({"type": "message", "role": "user", "content": "First"}),
            "",  # Empty line
            json.dumps({"type": "message", "role": "model", "content": "Second"}),
        ]

        msgs = parser.parse_lines(lines, start_index=0)
        assert len(msgs) == 2
        assert msgs[0].index == 0
        assert msgs[0].role == "user"
        assert msgs[1].index == 1
        assert msgs[1].role == "assistant"

    def test_gemini_extract_usage(self, parser) -> None:
        """Test _extract_usage with usageMetadata."""
        line = json.dumps(
            {
                "type": "message",
                "role": "model",
                "content": "Response",
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 50,
                },
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.usage is not None
        assert msg.usage.input_tokens == 100
        assert msg.usage.output_tokens == 50

    def test_gemini_extract_usage_no_usage(self, parser) -> None:
        """Test _extract_usage returns None without usageMetadata."""
        line = json.dumps({"type": "message", "role": "model", "content": "Response"})
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.usage is None

    def test_gemini_timestamp_invalid_format(self, parser) -> None:
        """Test handling of invalid timestamp format."""
        line = json.dumps(
            {
                "type": "message",
                "role": "user",
                "content": "Hello",
                "timestamp": "invalid-date",
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        # Should use default timestamp without crashing
        assert msg.timestamp is not None

    def test_gemini_content_none(self, parser) -> None:
        """Test handling of None content."""
        line = json.dumps({"type": "message", "role": "user", "content": None})
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content == ""

    def test_gemini_content_list_with_non_dict_function_call(self, parser) -> None:
        """Test parse_line handles functionCall that is not a dict (e.g. list)."""
        line = json.dumps(
            {
                "type": "message",
                "role": "model",
                "content": [
                    {"text": "some text"},
                    {"functionCall": [{"name": "tool", "args": {}}]},
                ],
            }
        )
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content == "some text"
        # functionCall was a list, not dict — should be skipped safely
        assert msg.content_type == "text"
        assert msg.tool_name is None

    def test_gemini_parse_session_json_basic(self, parser) -> None:
        """Test parse_session_json with basic user/gemini messages."""
        data = {
            "sessionId": "abc-123",
            "projectHash": "deadbeef",
            "startTime": "2024-01-01T10:00:00Z",
            "lastUpdated": "2024-01-01T10:05:00Z",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "user",
                    "content": "Hello Gemini",
                },
                {
                    "id": "msg-2",
                    "timestamp": "2024-01-01T10:00:01Z",
                    "type": "gemini",
                    "content": "Hello! How can I help?",
                },
            ],
        }

        msgs = parser.parse_session_json(data)
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello Gemini"
        assert msgs[0].index == 0
        assert msgs[1].role == "assistant"
        assert msgs[1].content == "Hello! How can I help?"
        assert msgs[1].index == 1

    def test_gemini_parse_session_json_with_tool_calls(self, parser) -> None:
        """Test parse_session_json with gemini message containing toolCalls."""
        data = {
            "sessionId": "abc-123",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "gemini",
                    "content": "Let me read that file.",
                    "toolCalls": [
                        {
                            "name": "read_file",
                            "args": {"path": "test.txt"},
                            "result": [{"functionResponse": {"content": "file contents here"}}],
                        }
                    ],
                },
            ],
        }

        msgs = parser.parse_session_json(data)
        # Should produce: text response + tool_use + tool_result = 3 messages
        assert len(msgs) == 3
        assert msgs[0].role == "assistant"
        assert msgs[0].content_type == "text"
        assert msgs[0].content == "Let me read that file."
        assert msgs[1].role == "assistant"
        assert msgs[1].content_type == "tool_use"
        assert msgs[1].tool_name == "read_file"
        assert msgs[1].tool_input == {"path": "test.txt"}
        assert msgs[2].role == "tool"
        assert msgs[2].content_type == "tool_result"
        assert msgs[2].tool_name == "read_file"

    def test_gemini_parse_session_json_skips_info_warning(self, parser) -> None:
        """Test that info and warning messages are skipped."""
        data = {
            "sessionId": "abc-123",
            "messages": [
                {"id": "1", "timestamp": "2024-01-01T10:00:00Z", "type": "user", "content": "Hi"},
                {
                    "id": "2",
                    "timestamp": "2024-01-01T10:00:01Z",
                    "type": "info",
                    "content": "Info msg",
                },
                {
                    "id": "3",
                    "timestamp": "2024-01-01T10:00:02Z",
                    "type": "warning",
                    "content": "Warn msg",
                },
                {
                    "id": "4",
                    "timestamp": "2024-01-01T10:00:03Z",
                    "type": "gemini",
                    "content": "Response",
                },
            ],
        }

        msgs = parser.parse_session_json(data)
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

    def test_gemini_parse_session_json_empty_messages(self, parser) -> None:
        """Test parse_session_json with no messages."""
        data = {"sessionId": "abc-123", "messages": []}
        msgs = parser.parse_session_json(data)
        assert msgs == []

    def test_gemini_parse_session_json_no_messages_key(self, parser) -> None:
        """Test parse_session_json with missing messages key."""
        data = {"sessionId": "abc-123"}
        msgs = parser.parse_session_json(data)
        assert msgs == []

    def test_gemini_parse_session_json_gemini_no_content(self, parser) -> None:
        """Test gemini message with no text content but with toolCalls."""
        data = {
            "sessionId": "abc-123",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {"name": "bash", "args": {"command": "ls"}},
                    ],
                },
            ],
        }

        msgs = parser.parse_session_json(data)
        # No text content, but should have tool_use
        assert len(msgs) == 1
        assert msgs[0].content_type == "tool_use"
        assert msgs[0].tool_name == "bash"

    def test_gemini_parse_session_json_indexes_are_sequential(self, parser) -> None:
        """Test that indexes across messages with tool calls are sequential."""
        data = {
            "sessionId": "abc-123",
            "messages": [
                {
                    "id": "1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "user",
                    "content": "Do it",
                },
                {
                    "id": "2",
                    "timestamp": "2024-01-01T10:00:01Z",
                    "type": "gemini",
                    "content": "OK",
                    "toolCalls": [
                        {"name": "tool1", "args": {}, "result": [{"functionResponse": "r1"}]},
                        {"name": "tool2", "args": {}, "result": [{"functionResponse": "r2"}]},
                    ],
                },
                {
                    "id": "3",
                    "timestamp": "2024-01-01T10:00:02Z",
                    "type": "user",
                    "content": "Thanks",
                },
            ],
        }

        msgs = parser.parse_session_json(data)
        # user(0), text(1), tool1_use(2), tool1_result(3), tool2_use(4), tool2_result(5), user(6)
        assert len(msgs) == 7
        for i, msg in enumerate(msgs):
            assert msg.index == i, f"Expected index {i}, got {msg.index} for {msg.content_type}"

    def test_gemini_parse_session_json_result_as_dict(self, parser) -> None:
        """Test parse_session_json handles result as dict (backwards compat)."""
        data = {
            "sessionId": "abc-123",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {
                            "name": "read_file",
                            "args": {"path": "test.txt"},
                            "result": {"functionResponse": {"content": "file contents"}},
                        }
                    ],
                },
            ],
        }

        msgs = parser.parse_session_json(data)
        assert len(msgs) == 2
        assert msgs[0].content_type == "tool_use"
        assert msgs[1].content_type == "tool_result"
        assert msgs[1].tool_name == "read_file"


class TestParserRegistry:
    """Tests for the parser registry and get_parser function."""

    def test_registry_has_correct_parsers(self) -> None:
        """Verify each source maps to the correct parser class."""
        assert PARSER_REGISTRY["claude"] is ClaudeTranscriptParser
        assert PARSER_REGISTRY["gemini"] is GeminiTranscriptParser
        assert PARSER_REGISTRY["codex"] is CodexTranscriptParser

    def test_get_parser_returns_correct_instances(self) -> None:
        """get_parser should return instances of the correct parser class."""
        assert isinstance(get_parser("claude"), ClaudeTranscriptParser)
        assert isinstance(get_parser("gemini"), GeminiTranscriptParser)
        assert isinstance(get_parser("codex"), CodexTranscriptParser)

    def test_get_parser_unknown_source_defaults_to_claude(self) -> None:
        """Unknown source should default to ClaudeTranscriptParser."""
        assert isinstance(get_parser("unknown-cli"), ClaudeTranscriptParser)
