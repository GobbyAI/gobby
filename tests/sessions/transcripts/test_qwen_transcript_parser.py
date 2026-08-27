"""Qwen current-envelope transcript parser coverage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.sessions.transcripts.base import BaseTranscriptParser, ParsedMessage, TokenUsage
from gobby.sessions.transcripts.qwen import QwenTranscriptParser, _result_content

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "transcripts" / "qwen"


def _load_json(path: str) -> Any:
    return json.loads((FIXTURE_DIR / path).read_text())


def _load_records() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (FIXTURE_DIR / "current_envelope.jsonl").read_text().splitlines()
    ]


def _usage_dict(usage: TokenUsage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }


def _normalized(message: ParsedMessage) -> dict[str, Any]:
    return {
        "index": message.index,
        "role": message.role,
        "content": message.content,
        "content_type": message.content_type,
        "tool_name": message.tool_name,
        "tool_input": message.tool_input,
        "tool_result": message.tool_result,
        "usage": _usage_dict(message.usage),
        "model": message.model,
        "tool_use_id": message.tool_use_id,
        "message_id": message.message_id,
        "timestamp": message.timestamp.isoformat(),
    }


def test_qwen_parser_exposes_only_the_current_jsonl_contract() -> None:
    assert issubclass(QwenTranscriptParser, BaseTranscriptParser)
    assert not hasattr(QwenTranscriptParser(), "parse_session_json")


def test_qwen_current_jsonl_matches_golden_fixture() -> None:
    parser = QwenTranscriptParser(session_id="qwen-session")

    actual = [
        _normalized(message)
        for message in parser.parse_lines(
            (FIXTURE_DIR / "current_envelope.jsonl").read_text().splitlines()
        )
    ]

    assert actual == _load_json("current_envelope_expected.json")


def test_qwen_usage_is_owned_once_by_each_assistant_envelope() -> None:
    messages = QwenTranscriptParser().parse_lines(
        (FIXTURE_DIR / "current_envelope.jsonl").read_text().splitlines()
    )
    assistant_usage = [
        _usage_dict(message.usage) for message in messages if message.role == "assistant"
    ]

    assert assistant_usage == [
        {
            "input_tokens": 75,
            "output_tokens": 25,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 25,
        },
        None,
        None,
        None,
    ]


def test_qwen_known_system_metadata_is_suppressed() -> None:
    metadata = [
        record
        for record in _load_records()
        if record.get("subtype") in {"file_history_snapshot", "ui_telemetry"}
    ]
    parser = QwenTranscriptParser()

    assert metadata
    assert [parser.parse_line(json.dumps(record), 99) for record in metadata] == [None, None]


def test_qwen_unknown_record_type_and_system_subtype_remain_visible() -> None:
    messages = QwenTranscriptParser().parse_lines(
        (FIXTURE_DIR / "current_envelope.jsonl").read_text().splitlines()
    )

    assert [(message.role, message.content_type) for message in messages[-2:]] == [
        ("system", "custom_payload"),
        ("assistant", "custom_event"),
    ]


def test_qwen_extract_last_messages_reads_nested_parts() -> None:
    messages = QwenTranscriptParser().extract_last_messages(_load_records(), num_pairs=1)

    assert messages == [
        {"role": "user", "content": "Explain JSON."},
        {
            "role": "assistant",
            "content": "I will inspect the project.\n[Tool call: read_file]",
        },
    ]


def test_result_content_keeps_payloads_longer_than_500_characters() -> None:
    text = "x" * 612
    payload = {"body": "y" * 580}

    assert _result_content(text) == text
    assert _result_content(payload) == json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    assert _result_content(None) == ""


def test_qwen_failed_function_response_in_ledger() -> None:
    calls = [
        ("call-error", "error-id"),
        ("call-cancelled", "cancelled-id"),
        ("call-response", "response-error"),
        ("call-none", "none-is-success"),
        ("call-statusless", "statusless-error"),
    ]
    turns = [
        {"type": "user", "message": {"parts": [{"text": "inspect"}]}},
        {
            "type": "assistant",
            "message": {
                "parts": [
                    {
                        "functionCall": {
                            "id": call_id,
                            "name": "Read",
                            "args": {"path": path},
                        }
                    }
                    for call_id, path in calls
                ]
            },
        },
        {
            "type": "tool_result",
            "toolCallResult": {"callId": "call-error", "status": "error"},
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-error",
                            "name": "Read",
                            "response": {"output": "permission denied"},
                        }
                    }
                ]
            },
        },
        {
            "type": "tool_result",
            "toolCallResult": {"status": "cancelled"},
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-cancelled",
                            "name": "Read",
                            "response": {"output": "cancelled by user"},
                        }
                    }
                ]
            },
        },
        {
            "type": "tool_result",
            "toolCallResult": {"callId": "call-response", "status": "completed"},
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "name": "Read",
                            "response": {"error": "response-only failure"},
                        }
                    }
                ]
            },
        },
        {
            "type": "tool_result",
            "toolCallResult": {"callId": "call-none", "status": "completed"},
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "name": "Read",
                            "response": {"error": None},
                        }
                    }
                ]
            },
        },
        {
            "type": "tool_result",
            "toolCallResult": {"callId": "call-2", "status": "cancelled"},
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "name": "Read",
                            "response": {"output": "cancelled by user"},
                        }
                    }
                ]
            },
        },
        {
            "type": "tool_result",
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-3",
                            "name": "Read",
                            "response": {"error": "quota exceeded"},
                        }
                    }
                ]
            },
        },
        {
            "type": "tool_result",
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-statusless",
                            "name": "Read",
                            "response": {"error": "quota exceeded"},
                        }
                    }
                ]
            },
        },
        {"type": "assistant", "message": {"parts": [{"text": "done"}]}},
    ]

    messages = QwenTranscriptParser().extract_last_messages(turns, include_tool_activity=True)

    assert messages[0]["tool_activity"].splitlines() == [
        "[tool activity]",
        "- Read error-id ! failed: permission denied",
        "- Read cancelled-id ! failed: cancelled by user",
        "- Read response-error ! failed: response-only failure",
        "- Read none-is-success",
        "- Read statusless-error ! failed: quota exceeded",
    ]
