"""
Tests for transcript parsers (Claude, Codex, Qwen, Droid, and Grok).
Consolidated from individual files.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.sessions.message_stats import compute_message_stats
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_parsing import _get_parser
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcripts import PARSER_REGISTRY, get_parser
from gobby.sessions.transcripts.base import (
    UNMODELED_RECORD_CONTENT_TYPE,
    ParsedMessage,
    ParsedToolEvent,
    TranscriptParser,
)
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser

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

    def test_parse_line_uses_current_time_for_non_string_timestamp(self, parser) -> None:
        before = datetime.now(UTC)
        line = json.dumps(
            {
                "type": "user",
                "message": {"content": "Hello world"},
                "timestamp": {"malformed": True},
            }
        )

        msg = parser.parse_line(line, 0)

        assert msg is not None
        assert before <= msg.timestamp <= datetime.now(UTC)

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
        """An unrecognized record-level type becomes a non-rendering sentinel
        (routed to the T2 worklist at render time, not surfaced as a card)."""
        line = json.dumps({"type": "unknown_event"})
        msg = parser.parse_line(line, 0)
        assert msg is not None
        assert msg.content_type == UNMODELED_RECORD_CONTENT_TYPE
        assert msg.role == "system"
        assert msg.content == "unknown_event"

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
                "message": {
                    "content": (
                        "<command-name>/clear</command-name>\n"
                        "<command-message>clear</command-message>"
                    )
                },
            }
        )

        # Quoted marker without the command-message sibling
        assert not parser.is_session_boundary(
            {
                "type": "user",
                "message": {"content": "quoted <command-name>/clear</command-name> marker"},
            }
        )

        # Tool results may quote the complete command without being a boundary
        assert not parser.is_session_boundary(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": (
                                "<command-name>/clear</command-name>\n"
                                "<command-message>clear</command-message>"
                            ),
                        }
                    ]
                },
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
            {
                "type": "user",
                "message": {
                    "content": (
                        "<command-name>/clear</command-name>\n"
                        "<command-message>clear</command-message>"
                    )
                },
            },
            {"type": "user", "message": {"content": "after1"}},
            {"type": "agent", "message": {"content": "after2"}},
        ]

        extracted = parser.extract_turns_since_clear(turns)
        assert len(extracted) == 2
        assert extracted[0]["message"]["content"] == "after1"

    def test_extract_turns_since_clear_consecutive(self, parser) -> None:
        turns = [
            {
                "type": "user",
                "message": {
                    "content": (
                        "<command-name>/clear</command-name>\n"
                        "<command-message>clear</command-message>"
                    )
                },
            },
            {
                "type": "user",
                "message": {
                    "content": (
                        "<command-name>/clear</command-name>\n"
                        "<command-message>clear</command-message>"
                    )
                },
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


class TestClaudeRecordEnvelopes:
    """Record-level envelope handling: session-metadata records are recognized
    and not surfaced as cards, compaction boundaries are first-classed, and a
    genuinely-unknown record type becomes a non-rendering sentinel
    (content_type=unmodeled_record) routed to the T2 observation worklist while
    also being recorded in parser-error.log."""

    @pytest.fixture
    def parser(self):
        return ClaudeTranscriptParser(session_id="probe")

    @pytest.mark.parametrize(
        "record_type",
        [
            "queue-operation",
            "last-prompt",
            "attachment",
            "agent-name",
            "mode",
            "permission-mode",
            "pr-link",
            "started",
            "result",
            "worktree-state",
            "fork-context-ref",
            "summary",
            "file-history-snapshot",
            "custom-title",
            "atis-latch",
        ],
    )
    def test_known_envelope_records_are_dropped(
        self, parser, record_type, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logged: list[str] = []
        monkeypatch.setattr(
            parser.error_log,
            "log_unknown_block",
            lambda _index, _session, block_type, _raw: logged.append(block_type),
        )
        line = json.dumps({"type": record_type, "foo": "bar", "timestamp": "2024-01-01T12:00:00Z"})
        assert parser._expand_line(line, 0) == []
        assert parser.parse_line(line, 0) is None
        assert logged == []

    def test_queued_command_attachment_emits_user_message(self, parser) -> None:
        data = {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "prompt": "I'm going to bed. Just keep the work in Fable please.",
            },
            "timestamp": "2024-01-01T12:00:00Z",
        }
        line = json.dumps(data)

        messages = parser._expand_line(line, 4)
        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].content == data["attachment"]["prompt"]
        assert messages[0].raw_json == data

        message = parser.parse_line(line, 4)
        assert message is not None
        assert message.role == "user"
        assert message.content == data["attachment"]["prompt"]

    def test_queued_command_attachment_is_in_last_messages(self, parser) -> None:
        turns = [
            {
                "type": "attachment",
                "attachment": {"type": "queued_command", "prompt": "queued instruction"},
            },
            {"type": "assistant", "message": {"role": "assistant", "content": "done"}},
        ]

        assert parser.extract_last_messages(turns, num_pairs=1) == [
            {"role": "user", "content": "queued instruction"},
            {"role": "assistant", "content": "done"},
        ]

    @pytest.mark.parametrize("prompt", [None, "", "   "])
    def test_empty_queued_command_attachment_is_dropped(self, parser, prompt) -> None:
        line = json.dumps(
            {
                "type": "attachment",
                "attachment": {"type": "queued_command", "prompt": prompt},
            }
        )

        assert parser._expand_line(line, 0) == []
        assert parser.parse_line(line, 0) is None

    def test_ai_title_emits_session_title(self, parser) -> None:
        """ai-title records emit a ParsedMessage with content_type=session_title."""
        line = json.dumps(
            {
                "type": "ai-title",
                "aiTitle": "Fix authentication bug",
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 1
        assert msgs[0].content_type == "session_title"
        assert msgs[0].role == "system"
        assert msgs[0].content == "Fix authentication bug"

        single = parser.parse_line(line, 0)
        assert single is not None
        assert single.content_type == "session_title"
        assert single.content == "Fix authentication bug"

    def test_ai_title_empty_is_dropped(self, parser) -> None:
        """ai-title with missing or empty aiTitle is silently dropped."""
        line = json.dumps({"type": "ai-title", "timestamp": "2024-01-01T12:00:00Z"})
        assert parser._expand_line(line, 0) == []
        assert parser.parse_line(line, 0) is None

    def test_system_metadata_record_is_dropped(self, parser) -> None:
        line = json.dumps(
            {
                "type": "system",
                "subtype": "stop_hook_summary",
                "hookCount": 1,
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        assert parser._expand_line(line, 0) == []
        assert parser.parse_line(line, 0) is None

    def test_compact_boundary_is_first_classed(self, parser) -> None:
        line = json.dumps(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "content": "Conversation compacted",
                "compactMetadata": {"trigger": "manual", "preTokens": 266101},
                "uuid": "u1",
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 1
        block = msgs[0]
        assert block.role == "system"
        assert block.content_type == "compaction_summary"
        assert block.content == "Conversation compacted (manual)"
        assert block.tool_use_id == "u1"  # keyed for render dedup
        assert block.raw_json["compactMetadata"]["preTokens"] == 266101

        single = parser.parse_line(line, 0)
        assert single is not None
        assert single.content_type == "compaction_summary"
        assert single.content == "Conversation compacted (manual)"
        assert single.tool_use_id == "u1"
        assert single.raw_json["compactMetadata"]["preTokens"] == 266101

    @pytest.mark.parametrize("flag", ["isCompactSummary", "isMeta"])
    def test_synthetic_user_entries_are_dropped(self, parser, flag: str) -> None:
        line = json.dumps(
            {
                "type": "user",
                flag: True,
                "message": {"role": "user", "content": "synthetic content"},
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

        assert parser._expand_line(line, 0) == []
        assert parser.parse_line(line, 0) is None

    @pytest.mark.parametrize(
        ("record", "expected_content", "expected_model"),
        [
            (
                {
                    "type": "system",
                    "subtype": "api_error",
                    "error": {"formatted": "429 overloaded"},
                },
                "429 overloaded",
                None,
            ),
            (
                {
                    "type": "system",
                    "subtype": "model_refusal_fallback",
                    "content": "Switching models",
                    "originalModel": "claude-fable-5",
                    "fallbackModel": "claude-opus-4-8",
                },
                "Switching models",
                "claude-opus-4-8",
            ),
        ],
    )
    def test_system_error_and_fallback_records_are_emitted(
        self,
        parser,
        record: dict[str, object],
        expected_content: str,
        expected_model: str | None,
    ) -> None:
        record["timestamp"] = "2024-01-01T12:00:00Z"
        line = json.dumps(record)

        expanded = parser._expand_line(line, 0)
        assert len(expanded) == 1
        assert expanded[0].role == "system"
        assert expanded[0].content_type == "text"
        assert expanded[0].content == expected_content
        assert expanded[0].model == expected_model
        assert expanded[0].raw_json["subtype"] == record["subtype"]
        assert compute_message_stats(expanded)["message_count"] == 1

        single = parser.parse_line(line, 0)
        assert single is not None
        assert single.role == "system"
        assert single.content == expected_content

    def test_compact_boundary_without_metadata_uses_default_text(self, parser) -> None:
        line = json.dumps(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "u9",
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 1
        assert msgs[0].content == "Conversation compacted"

    def test_unknown_record_type_emits_sentinel_and_logs(self, parser, monkeypatch) -> None:
        """Unknown records use both discovery channels and remain non-rendering."""
        calls: list[tuple] = []
        monkeypatch.setattr(
            parser.error_log,
            "log_unknown_block",
            lambda *a, **k: calls.append((a, k)),
        )
        data = {"type": "brand-new-envelope", "x": 1, "timestamp": "2024-01-01T12:00:00Z"}
        line = json.dumps(data)

        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 1
        sentinel = msgs[0]
        assert sentinel.content_type == UNMODELED_RECORD_CONTENT_TYPE
        assert sentinel.role == "system"
        assert sentinel.content == "brand-new-envelope"  # real type rides in content
        assert sentinel.raw_json == data
        # Provenance is left None pre-annotation (annotate_record_source fills it
        # on the events path; the direct parse_line fallback resolves from index).
        assert sentinel.source is None
        assert sentinel.source_line is None
        assert sentinel.source_ref is None

        single = parser.parse_line(line, 0)
        assert single is not None
        assert single.content_type == UNMODELED_RECORD_CONTENT_TYPE
        assert single.role == "system"
        assert single.content == "brand-new-envelope"
        assert single.raw_json == data

        expected_call = ((0, "probe", "brand-new-envelope", data), {})
        assert calls == [expected_call, expected_call]

    def test_block_level_unknown_content_still_passes_through(self, parser) -> None:
        """Regression guard: record-level changes must not disturb block-level
        fail-soft. An unknown content block inside an assistant message still
        surfaces with its original content_type."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "before"},
                        {"type": "mystery_block", "data": 1},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )
        msgs = parser._expand_line(line, 0)
        assert "mystery_block" in [m.content_type for m in msgs]

    def test_repeated_compactions_render_distinct_dividers(self, parser) -> None:
        lines = [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "content": "Conversation compacted",
                    "compactMetadata": {"trigger": "manual"},
                    "uuid": "u1",
                    "timestamp": "2024-01-01T12:00:00Z",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "mid"}]},
                    "timestamp": "2024-01-01T12:00:01Z",
                }
            ),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "content": "Conversation compacted",
                    "compactMetadata": {"trigger": "auto"},
                    "uuid": "u2",
                    "timestamp": "2024-01-01T12:00:02Z",
                }
            ),
        ]
        msgs = [m for m in parser.parse_lines(lines) if isinstance(m, ParsedMessage)]
        rendered = render_transcript(msgs, session_id="probe", source="claude")
        dividers = [
            block
            for group in rendered
            for block in group.content_blocks
            if block.type == "compaction_summary"
        ]
        assert [b.content for b in dividers] == [
            "Conversation compacted (manual)",
            "Conversation compacted (auto)",
        ]
        assert not [
            block for group in rendered for block in group.content_blocks if block.type == "unknown"
        ]


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

    def test_expand_assistant_fallback_block_as_system_record(self, parser) -> None:
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-4-8",
                    "content": [
                        {
                            "type": "fallback",
                            "from": {"model": "claude-fable-5"},
                            "to": {"model": "claude-opus-4-8"},
                        }
                    ],
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

        expanded = parser._expand_line(line, 0)
        assert len(expanded) == 1
        assert expanded[0].role == "system"
        assert expanded[0].content_type == "text"
        assert expanded[0].content == "Model fallback: claude-fable-5 -> claude-opus-4-8"
        assert expanded[0].model == "claude-opus-4-8"
        assert compute_message_stats(expanded)["message_count"] == 1

        single = parser.parse_line(line, 0)
        assert single is not None
        assert single.role == "system"
        assert single.content == expanded[0].content

    def test_expand_user_image_and_document_blocks(self, parser) -> None:
        image_source = {"type": "base64", "media_type": "image/png", "data": "abc"}
        document_source = {"type": "base64", "media_type": "application/pdf", "data": "pdf"}
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "image", "source": image_source},
                        {"type": "document", "source": document_source, "title": "notes.pdf"},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

        msgs = parser._expand_line(line, 0)
        rendered = render_transcript(msgs)

        assert [msg.content_type for msg in msgs] == ["image", "document"]
        assert msgs[0].content == image_source
        assert msgs[1].content == {**document_source, "name": "notes.pdf"}
        blocks = [block for message in rendered for block in message.content_blocks]
        assert [block.type for block in blocks] == ["image", "document"]
        assert blocks[0].source == image_source
        assert blocks[1].source == {**document_source, "name": "notes.pdf"}

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

    def test_parse_lines_reads_hook_blocking_attachment_fixture(self, parser) -> None:
        """A scrubbed Claude Code capture emits one hook-block tool result."""
        fixture = (
            Path(__file__).parents[1]
            / "fixtures"
            / "transcripts"
            / "claude-hook-blocking-error.jsonl"
        )
        msgs = parser.parse_lines(fixture.read_text().splitlines())

        assert len(msgs) == 1
        msg = msgs[0]
        assert isinstance(msg, ParsedMessage)
        assert msg.role == "tool"
        assert msg.content_type == "tool_result"
        assert msg.tool_name == "Stop"
        assert msg.tool_use_id == "33333333-3333-3333-3333-333333333333"
        assert msg.content == (
            "Rule enforced by Gobby: [require-task-close]\nTask #16260 is still open."
        )
        assert msg.tool_result == {"content": msg.content, "is_error": True}

    def test_expand_unknown_record_type_emits_sentinel(self, parser) -> None:
        """An unrecognized record-level type becomes a non-rendering sentinel
        (session envelopes are not conversation content, so no card — but the
        discovery signal flows to the T2 worklist). Block-level fail-soft is
        covered separately."""
        line = json.dumps({"type": "progress", "timestamp": "2024-01-01T12:00:00Z"})
        msgs = parser._expand_line(line, 0)
        assert len(msgs) == 1
        assert msgs[0].content_type == UNMODELED_RECORD_CONTENT_TYPE
        assert msgs[0].content == "progress"

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
    def parser(self) -> CodexTranscriptParser:
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
    def _function_call_output(
        call_id: str,
        output: Any,
        ts: str = "2024-06-15T10:30:00Z",
    ) -> str:
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
    def _tool_search_call(
        arguments: dict[str, Any],
        call_id: str | None = "call-search",
        response_id: str | None = "tsc-1",
        ts: str = "2024-06-15T10:30:00Z",
    ) -> str:
        payload: dict[str, Any] = {
            "type": "tool_search_call",
            "arguments": arguments,
            "status": "completed",
        }
        if call_id is not None:
            payload["call_id"] = call_id
        if response_id is not None:
            payload["id"] = response_id
        return json.dumps({"timestamp": ts, "type": "response_item", "payload": payload})

    @staticmethod
    def _tool_search_output(
        output: dict[str, Any],
        call_id: str | None = "call-search",
        ts: str = "2024-06-15T10:30:00Z",
    ) -> str:
        payload: dict[str, Any] = {"type": "tool_search_output", **output}
        if call_id is not None:
            payload["call_id"] = call_id
        return json.dumps({"timestamp": ts, "type": "response_item", "payload": payload})

    @staticmethod
    def _reasoning(
        ts: str = "2024-06-15T10:30:00Z",
        **extra: Any,
    ) -> str:
        """Build a Codex response_item/reasoning envelope line."""
        return json.dumps(
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [],
                    "content": None,
                    **extra,
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
        return json.dumps({"timestamp": ts, "type": "session_meta", "payload": {"id": session_id}})

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
        line = self._function_call("exec_command", '{"cmd":"ls","workdir":"/tmp"}', "call_abc")
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

    def test_parse_function_call_output_preserves_structured_payloads(self, parser) -> None:
        payload = {
            "success": False,
            "result": {
                "success": False,
                "error": "Rule enforced by Gobby: [consecutive-tool-block]",
                "error_code": "TOOL_BLOCKED",
            },
            "response_time_ms": 1.59,
        }
        line = self._function_call_output("call_err", payload)
        msg = parser.parse_line(line, 0)

        assert msg is not None
        assert msg.tool_use_id == "call_err"
        assert msg.tool_result == payload
        assert msg.content == str(payload)

    # -- parse_line: skipped types --

    def test_skip_event_msg(self, parser) -> None:
        line = self._event_msg("task_started", turn_id="turn-1")
        assert parser.parse_line(line, 0) is None

    def test_skip_session_meta(self, parser) -> None:
        assert parser.parse_line(self._session_meta(), 0) is None

    def test_skip_turn_context(self, parser) -> None:
        assert parser.parse_line(self._turn_context(), 0) is None

    # -- parse_line: error handling --

    def test_parse_line_reasoning_is_ignored_without_unknown_warning(
        self, parser, monkeypatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def _log_unknown_block(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(parser.error_log, "log_unknown_block", _log_unknown_block)

        assert parser.parse_line(self._reasoning(), 0) is None
        assert calls == []

    def test_parse_line_tool_search_metadata_is_ignored_without_unknown_warning(
        self, parser, monkeypatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def _log_unknown_block(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(parser.error_log, "log_unknown_block", _log_unknown_block)

        call = parser.parse_line(
            self._tool_search_call(
                {"query": "gobby-skills list_mcp_servers", "limit": 8},
                call_id="call-search",
                response_id="tsc-1",
            ),
            0,
        )
        assert isinstance(call, ParsedMessage)
        assert call.content_type == "tool_use"
        assert call.tool_name == "tool_search"
        assert call.tool_input == {"query": "gobby-skills list_mcp_servers", "limit": 8}
        assert call.tool_use_id == "call-search"
        assert call.message_id == "tsc-1"

        result = parser.parse_line(
            self._tool_search_output(
                {
                    "tools": [{"name": "mcp__gobby", "type": "namespace", "tool_count": 8}],
                    "tools_count": 1,
                    "status": "completed",
                },
                call_id=None,
            ),
            1,
        )
        assert isinstance(result, ParsedMessage)
        assert result.content_type == "tool_result"
        assert result.tool_name == "tool_search"
        assert result.tool_use_id == "call-search"
        assert result.tool_result == {
            "tools": [{"name": "mcp__gobby", "type": "namespace", "tool_count": 8}],
            "tools_count": 1,
            "status": "completed",
        }
        assert calls == []

    def test_parse_line_tool_search_standalone_output_uses_fallback_id(self, parser) -> None:
        result = parser.parse_line(
            self._tool_search_output(
                {"tools": [{"name": "mcp__gobby", "type": "namespace"}], "tools_count": 1},
                call_id=None,
            ),
            7,
        )

        assert isinstance(result, ParsedMessage)
        assert result.content_type == "tool_result"
        assert result.tool_name == "tool_search"
        assert result.tool_use_id == "tool-search-7"
        assert result.tool_result == {
            "tools": [{"name": "mcp__gobby", "type": "namespace"}],
            "tools_count": 1,
        }

    def test_parse_line_tool_search_outputs_without_ids_pair_fifo(self, parser) -> None:
        parser.parse_line(
            self._tool_search_call({"query": "first"}, call_id="call-first", response_id="tsc-1"),
            1,
        )
        parser.parse_line(
            self._tool_search_call({"query": "second"}, call_id="call-second", response_id="tsc-2"),
            2,
        )

        first_result = parser.parse_line(
            self._tool_search_output({"tools_count": 1}, call_id=None),
            3,
        )
        second_result = parser.parse_line(
            self._tool_search_output({"tools_count": 2}, call_id=None),
            4,
        )

        assert isinstance(first_result, ParsedMessage)
        assert isinstance(second_result, ParsedMessage)
        assert first_result.tool_use_id == "call-first"
        assert second_result.tool_use_id == "call-second"

    def test_tool_search_pending_queue_survives_parser_hydration(
        self, parser: CodexTranscriptParser
    ) -> None:
        parser.parse_line(
            self._tool_search_call({"query": "first"}, call_id="call-first", response_id="tsc-1"),
            1,
        )
        state = parser.snapshot_state()
        resumed = CodexTranscriptParser()
        resumed.hydrate_state(state)

        result = resumed.parse_line(
            self._tool_search_output({"tools_count": 1}, call_id=None),
            2,
        )

        assert isinstance(result, ParsedMessage)
        assert result.tool_use_id == "call-first"

    def test_tool_search_windowed_hydration_replay_dedupes_pending_ids(
        self, parser: CodexTranscriptParser
    ) -> None:
        call_line = self._tool_search_call(
            {"query": "first"},
            call_id="call-first",
            response_id="tsc-1",
        )
        parser.parse_line(call_line, 1)
        state = parser.snapshot_state()
        resumed = CodexTranscriptParser()
        resumed.hydrate_state(state)

        resumed.parse_line(call_line, 1)
        first_result = resumed.parse_line(
            self._tool_search_output({"tools_count": 1}, call_id=None),
            2,
        )
        fallback_result = resumed.parse_line(
            self._tool_search_output({"tools_count": 0}, call_id=None),
            3,
        )

        assert isinstance(first_result, ParsedMessage)
        assert isinstance(fallback_result, ParsedMessage)
        assert first_result.tool_use_id == "call-first"
        assert fallback_result.tool_use_id == "tool-search-3"

    def test_tool_search_explicit_out_of_order_output_removes_pending_id(
        self, parser: CodexTranscriptParser
    ) -> None:
        parser.parse_line(
            self._tool_search_call({"query": "first"}, call_id="call-first", response_id="tsc-1"),
            1,
        )
        parser.parse_line(
            self._tool_search_call({"query": "second"}, call_id="call-second", response_id="tsc-2"),
            2,
        )

        second_result = parser.parse_line(
            self._tool_search_output({"tools_count": 2}, call_id="call-second"),
            3,
        )
        first_result = parser.parse_line(
            self._tool_search_output({"tools_count": 1}, call_id=None),
            4,
        )
        fallback_result = parser.parse_line(
            self._tool_search_output({"tools_count": 0}, call_id=None),
            5,
        )

        assert isinstance(second_result, ParsedMessage)
        assert isinstance(first_result, ParsedMessage)
        assert isinstance(fallback_result, ParsedMessage)
        assert second_result.tool_use_id == "call-second"
        assert first_result.tool_use_id == "call-first"
        assert fallback_result.tool_use_id == "tool-search-5"

    def test_tool_search_pair_normalizes_and_renders_as_tool_chain(self, parser) -> None:
        records = parser.parse_lines(
            [
                self._tool_search_call(
                    {"query": "mcp__gobby list_tools", "limit": 3},
                    call_id="call-search",
                    response_id="tsc-1",
                ),
                self._tool_search_output(
                    {
                        "tools": [{"name": "mcp__gobby", "type": "namespace", "tool_count": 8}],
                        "tools_count": 1,
                    },
                    call_id="call-search",
                ),
            ]
        )
        normalized = normalize_transcript_records(records, source="codex")
        messages: list[ParsedMessage] = []
        for record in normalized:
            assert isinstance(record, ParsedMessage)
            messages.append(record)

        rendered = render_transcript(messages, cli_name="codex", source="codex")

        assert len(rendered) == 1
        block = rendered[0].content_blocks[0]
        assert block.type == "tool_chain"
        assert block.tool_calls is not None
        tool_call = block.tool_calls[0]
        assert tool_call.id == "call-search"
        assert tool_call.tool_name == "tool_search"
        assert tool_call.tool_type == "search"
        assert tool_call.result is not None
        assert tool_call.result.content == {
            "tools": [{"name": "mcp__gobby", "type": "namespace", "tool_count": 8}],
            "tools_count": 1,
        }

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

    def test_parse_lines_skips_reasoning_without_consuming_index(self, parser) -> None:
        lines = [
            self._msg("user", "First"),
            self._reasoning(),
            self._msg("assistant", "Second"),
        ]

        records = parser.parse_lines(lines, start_index=10)

        assert len(records) == 2
        messages = [r for r in records if isinstance(r, ParsedMessage)]

        assert [m.index for m in messages] == [10, 11]
        assert [m.content for m in messages] == ["First", "Second"]

    # -- mcp_tool_call_* event_msg parsing --

    def test_parse_mcp_tool_call_begin(self, parser) -> None:
        line = self._event_msg(
            "mcp_tool_call_begin",
            call_id="call_abc",
            invocation={
                "server": "gobby",
                "tool": "get_tool_schema",
                "arguments": {
                    "server_name": "gobby-tasks",
                    "tool_name": "create_task",
                    "session_id": "#2995",
                },
            },
        )
        record = parser.parse_line(line, 0)

        assert isinstance(record, ParsedToolEvent)
        assert record.phase == "begin"
        assert record.call_id == "call_abc"
        assert record.server == "gobby"
        assert record.tool == "get_tool_schema"
        assert record.arguments == {
            "server_name": "gobby-tasks",
            "tool_name": "create_task",
            "session_id": "#2995",
        }
        assert record.result is None
        assert record.error is None

    def test_parse_mcp_tool_call_end_ok(self, parser) -> None:
        line = self._event_msg(
            "mcp_tool_call_end",
            call_id="call_xyz",
            invocation={
                "server": "gobby",
                "tool": "get_tool_schema",
                "arguments": {"server_name": "gobby-tasks", "tool_name": "close_task"},
            },
            duration={"secs": 0, "nanos": 18_695_333},
            result={"Ok": {"content": [{"type": "text", "text": "{...}"}]}},
        )
        record = parser.parse_line(line, 0)

        assert isinstance(record, ParsedToolEvent)
        assert record.phase == "end"
        assert record.call_id == "call_xyz"
        assert record.server == "gobby"
        assert record.tool == "get_tool_schema"
        assert record.arguments == {
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }
        assert record.result == {"content": [{"type": "text", "text": "{...}"}]}
        assert record.error is None
        assert record.duration_ns == 18_695_333

    def test_parse_mcp_tool_call_end_err(self, parser) -> None:
        line = self._event_msg(
            "mcp_tool_call_end",
            call_id="call_err",
            invocation={
                "server": "gobby",
                "tool": "list_tools",
                "arguments": {"server_name": "context7"},
            },
            result={"Err": "transport closed"},
        )
        record = parser.parse_line(line, 0)

        assert isinstance(record, ParsedToolEvent)
        assert record.error == "transport closed"
        assert record.result is None

    def test_parse_mcp_tool_call_string_arguments(self, parser) -> None:
        line = self._event_msg(
            "mcp_tool_call_begin",
            call_id="call_str",
            invocation={
                "server": "gobby",
                "tool": "create_task",
                # Codex sometimes serializes inner arguments as a JSON string.
                "arguments": '{"title": "x", "category": "code"}',
            },
        )
        record = parser.parse_line(line, 0)

        assert isinstance(record, ParsedToolEvent)
        assert record.arguments == {"title": "x", "category": "code"}

    def test_parse_mcp_tool_call_missing_invocation_returns_none(self, parser) -> None:
        line = self._event_msg("mcp_tool_call_begin", call_id="call_x")
        assert parser.parse_line(line, 0) is None

    def test_parse_lines_mixes_messages_and_tool_events(self, parser) -> None:
        """Tool events must not consume the message-index counter."""
        lines = [
            self._msg("user", "First"),
            self._event_msg(
                "mcp_tool_call_begin",
                call_id="call_1",
                invocation={
                    "server": "gobby",
                    "tool": "get_tool_schema",
                    "arguments": {"server_name": "gobby-tasks", "tool_name": "create_task"},
                },
            ),
            self._event_msg(
                "mcp_tool_call_end",
                call_id="call_1",
                invocation={
                    "server": "gobby",
                    "tool": "get_tool_schema",
                    "arguments": {"server_name": "gobby-tasks", "tool_name": "create_task"},
                },
                result={"Ok": {"ok": True}},
            ),
            self._msg("assistant", "Second"),
        ]

        records = parser.parse_lines(lines, start_index=10)

        assert len(records) == 4
        messages = [r for r in records if isinstance(r, ParsedMessage)]
        tool_events = [r for r in records if isinstance(r, ParsedToolEvent)]

        assert [m.index for m in messages] == [10, 11]
        assert [m.content for m in messages] == ["First", "Second"]
        assert [e.phase for e in tool_events] == ["begin", "end"]

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

    def test_extract_last_messages_filters_instruction_dumps(self, parser) -> None:
        turns = [
            json.loads(self._msg("developer", "System instructions")),
            json.loads(self._msg("system", "System instructions")),
            json.loads(self._msg("user", "<user_instructions>synthetic dump</user_instructions>")),
            json.loads(self._msg("user", "AGENTS.md instructions for /tmp/project\n\n# Rules")),
            json.loads(self._msg("user", "hello")),
            json.loads(self._msg("assistant", "answer")),
        ]
        msgs = parser.extract_last_messages(turns, num_pairs=5)
        assert len(msgs) == 2
        assert msgs == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "answer"},
        ]

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
        line = json.dumps({"timestamp": "not-a-date", "type": "response_item", "payload": payload})
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

    def test_token_count_invalid_values_fall_back_to_zero(self, parser) -> None:
        line = self._event_msg(
            "token_count",
            input_tokens="bad",
            cached_input_tokens="oops",
            output_tokens=None,
            reasoning_output_tokens="nah",
            cache_creation_input_tokens="wat",
        )

        msg = parser.parse_line(line, 0)

        assert isinstance(msg, ParsedMessage)
        assert msg.usage is not None
        assert msg.usage.input_tokens == 0
        assert msg.usage.output_tokens == 0
        assert msg.usage.cache_creation_tokens == 0
        assert msg.usage.cache_read_tokens == 0

    def test_token_count_reads_nested_last_token_usage(self, parser) -> None:
        line = self._event_msg(
            "token_count",
            info={
                "last_token_usage": {
                    "input_tokens": 26_435,
                    "cached_input_tokens": 25_984,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 2,
                },
                "model_context_window": 258_400,
            },
        )

        msg = parser.parse_line(line, 0)

        assert msg is not None
        assert msg.usage is not None
        assert msg.usage.input_tokens == 451
        assert msg.usage.cache_read_tokens == 25_984
        assert msg.usage.output_tokens == 10
        assert msg.content_type == "usage"
        assert render_transcript([msg]) == []

    def test_token_count_zero_breakdown_reports_current_context_occupancy(
        self,
        parser: CodexTranscriptParser,
    ) -> None:
        line = self._event_msg(
            "token_count",
            info={
                "last_token_usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 7_248,
                },
                "model_context_window": 258_400,
            },
        )

        msg = parser.parse_line(line, 0)

        assert isinstance(msg, ParsedMessage)
        assert msg.usage is not None
        assert msg.usage.input_tokens == 0
        assert msg.usage.output_tokens == 0
        assert msg.usage.cache_creation_tokens == 0
        assert msg.usage.cache_read_tokens == 0
        assert msg.context_used_tokens == 7_248

    @pytest.mark.parametrize(
        "last_token_usage",
        [
            {},
            {"total_tokens": "7248"},
            {"total_tokens": -1},
            {"total_tokens": True},
            {"total_tokens": {"value": 7_248}},
        ],
    )
    def test_token_count_ignores_invalid_current_context_occupancy(
        self,
        parser: CodexTranscriptParser,
        last_token_usage: dict[str, Any],
    ) -> None:
        line = self._event_msg(
            "token_count",
            info={
                "last_token_usage": last_token_usage,
                "model_context_window": 258_400,
            },
        )

        msg = parser.parse_line(line, 0)

        assert isinstance(msg, ParsedMessage)
        assert msg.context_used_tokens is None


class TestGrokTranscriptParser:
    """Tests for Grok transcript parser."""

    def test_grok_usage_splits_cache_from_prompt_footprint(self) -> None:
        parser = GrokTranscriptParser(session_id="grok-session")
        line = json.dumps(
            {
                "timestamp": "2024-01-01T10:00:00Z",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": "Done"},
                        "usage": {
                            "inputTokens": 10_000,
                            "cachedInputTokens": 8_000,
                            "cacheCreationTokens": 500,
                            "outputTokens": 250,
                        },
                        "totalContextTokens": 512_000,
                    }
                },
            }
        )

        msg = parser.parse_line(line, 0)

        assert isinstance(msg, ParsedMessage)
        assert msg.usage is not None
        assert msg.usage.input_tokens == 1_500
        assert msg.usage.cache_read_tokens == 8_000
        assert msg.usage.cache_creation_tokens == 500
        assert msg.usage.output_tokens == 250
        assert msg.raw_json["params"]["update"]["totalContextTokens"] == 512_000


class TestParserRegistry:
    """Tests for the parser registry and get_parser function."""

    def test_registry_has_correct_parsers(self) -> None:
        """Verify each source maps to the correct parser class."""
        assert set(PARSER_REGISTRY) == {"claude", "grok", "qwen", "codex", "droid"}
        assert PARSER_REGISTRY["claude"] is ClaudeTranscriptParser
        assert PARSER_REGISTRY["grok"] is GrokTranscriptParser
        assert PARSER_REGISTRY["qwen"] is QwenTranscriptParser
        assert PARSER_REGISTRY["codex"] is CodexTranscriptParser
        assert PARSER_REGISTRY["droid"] is DroidTranscriptParser

    def test_get_parser_returns_correct_instances(self) -> None:
        """get_parser should return instances of the correct parser class."""
        assert isinstance(get_parser("claude"), ClaudeTranscriptParser)
        assert isinstance(get_parser("grok"), GrokTranscriptParser)
        assert isinstance(get_parser("qwen"), QwenTranscriptParser)
        assert isinstance(get_parser("codex"), CodexTranscriptParser)
        assert isinstance(get_parser("droid"), DroidTranscriptParser)
        assert isinstance(_get_parser("claude"), ClaudeTranscriptParser)

    def test_get_parser_threads_transcript_path_to_droid(self) -> None:
        """Droid parser construction keeps the transcript path for sidecar lookup."""
        transcript_path = Path("/tmp/fixture.jsonl")

        parser = get_parser("droid", session_id="session-id", transcript_path=transcript_path)

        assert isinstance(parser, DroidTranscriptParser)
        assert parser._transcript_path == transcript_path
        assert DroidTranscriptParser()._transcript_path is None

    @pytest.mark.parametrize("source", [None, "", "   ", "unknown-cli"])
    def test_get_parser_rejects_unknown_or_empty_source(self, source: str | None) -> None:
        with pytest.raises(ValueError, match="Unsupported transcript source"):
            get_parser(source)

    @pytest.mark.parametrize("source", ["", "   ", "unknown-cli"])
    def test_legacy_get_parser_rejects_unknown_or_empty_source(self, source: str) -> None:
        with pytest.raises(ValueError, match="Unsupported transcript source"):
            _get_parser(source)


def test_tool_activity_flag_preserves_pair_shape() -> None:
    parsers: list[TranscriptParser] = [
        ClaudeTranscriptParser(),
        CodexTranscriptParser(),
        GrokTranscriptParser(),
        QwenTranscriptParser(),
        DroidTranscriptParser(),
    ]
    for parser in parsers:
        assert parser.extract_last_messages([], include_tool_activity=True) == []

    turns: list[dict[str, Any]] = [
        {"message": {"role": "user", "content": "inspect it"}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "widget.py"},
                    }
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "contents",
                    }
                ],
            }
        },
        {"message": {"role": "assistant", "content": "done"}},
    ]
    parser = ClaudeTranscriptParser()
    without_ledger = parser.extract_last_messages(turns)
    with_ledger = parser.extract_last_messages(turns, include_tool_activity=True)

    assert [(message["role"], message["content"]) for message in with_ledger] == [
        (message["role"], message["content"]) for message in without_ledger
    ]
    assert with_ledger[0]["tool_activity"].splitlines() == [
        "[tool activity]",
        "- Read widget.py",
    ]
    assert "tool_activity" not in with_ledger[1]

    fixture_root = Path(__file__).parent / "transcripts" / "fixtures"
    fixture_parsers: list[tuple[TranscriptParser, Path]] = [
        *[
            (GrokTranscriptParser(), path)
            for path in sorted((fixture_root / "grok_audit").glob("*/updates.jsonl"))
        ],
        *[
            (DroidTranscriptParser(), path)
            for path in sorted((fixture_root / "droid").glob("*.jsonl"))
        ],
    ]
    assert fixture_parsers
    for fixture_parser, path in fixture_parsers:
        fixture_turns = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        plain = fixture_parser.extract_last_messages(fixture_turns, num_pairs=10_000)
        annotated = fixture_parser.extract_last_messages(
            fixture_turns,
            num_pairs=10_000,
            include_tool_activity=True,
        )

        assert [(message["role"], message["content"]) for message in annotated] == [
            (message["role"], message["content"]) for message in plain
        ], path
        assert all(
            "tool_activity" not in message or message["role"] == "user" for message in annotated
        ), path


def test_tool_only_turn_ledger_stays_on_its_user_message() -> None:
    turns = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "inspect"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read",
                    "toolCallId": "call-1",
                    "rawInput": {"path": "widget.py"},
                }
            },
        },
    ]

    without_ledger = GrokTranscriptParser().extract_last_messages(turns)
    with_ledger = GrokTranscriptParser().extract_last_messages(turns, include_tool_activity=True)

    assert [(item["role"], item["content"]) for item in without_ledger] == [
        ("user", "inspect"),
        ("assistant", ""),
    ]
    assert [(item["role"], item["content"]) for item in with_ledger] == [
        ("user", "inspect"),
        ("assistant", ""),
    ]
    assert "- Read widget.py (no result recorded)" in with_ledger[0]["tool_activity"]
    assert "tool_activity" not in with_ledger[1]


def _codex_text_message(role: str, text: str) -> dict[str, Any]:
    block_type = "input_text" if role == "user" else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
        },
    }


def test_codex_item_stream_precedence_in_ledger() -> None:
    turns = [
        _codex_text_message("user", "inspect"),
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "McpToolCall",
                    "server": "gobby",
                    "tool": "call_tool",
                    "arguments": {
                        "server_name": "gobby-tasks",
                        "tool_name": "claim_task",
                        "arguments": {"task_id": "#20728"},
                    },
                    "status": "completed",
                    "result": {"success": True},
                },
            },
        },
        _codex_text_message("assistant", "done"),
    ]

    messages = CodexTranscriptParser().extract_last_messages(turns, include_tool_activity=True)

    assert messages[0]["tool_activity"].splitlines() == [
        "[tool activity]",
        "- mcp gobby-tasks:claim_task task_id=#20728",
    ]


def test_codex_mixed_window_and_split_tail_precedence() -> None:
    command = "tail -f /var/log/widget.log"
    turns = [
        _codex_text_message("user", "inspect"),
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-1",
                "input": f'await tools.exec_command({{"cmd":{json.dumps(command)}}})',
            },
        },
        _codex_text_message("assistant", "waiting"),
    ]

    messages = CodexTranscriptParser().extract_last_messages(turns, include_tool_activity=True)

    assert messages[0]["tool_activity"].count(command) == 1
    assert "(no result recorded)" in messages[0]["tool_activity"]
    assert "await tools.exec_command" not in messages[0]["tool_activity"]


def test_codex_item_canonicalization_matches_exec_adapter() -> None:
    from gobby.sessions.transcripts.codex import _command_execution_outcomes
    from gobby.sessions.transcripts.codex_items import normalize_command_execution

    item = {
        "type": "CommandExecution",
        "id": "exec-1",
        "command": ["/bin/zsh", "-lc", "uv run pytest -k widget"],
        "exit_code": 1,
        "stderr": "failed",
    }
    normalized = normalize_command_execution(item)
    outcomes = _command_execution_outcomes(
        {}, {"type": "item_completed", "item": item}, datetime.now(UTC)
    )

    assert normalized is not None
    assert len(outcomes) == 1
    assert outcomes[0].command == normalized.command
    assert outcomes[0].result["success"] == normalized.success
