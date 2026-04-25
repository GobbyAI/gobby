from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.sessions.transcripts.codex import CodexTranscriptParser

pytestmark = pytest.mark.unit


def _response_item(payload: dict[str, Any], ts: str = "2024-06-15T10:30:00Z") -> str:
    return json.dumps({"timestamp": ts, "type": "response_item", "payload": payload})


def test_parse_web_search_call_as_tool_use() -> None:
    parser = CodexTranscriptParser()
    line = _response_item(
        {
            "type": "web_search_call",
            "status": "completed",
            "action": {
                "type": "search",
                "query": "pg_search docs",
                "queries": ["pg_search docs", "postgres pg_search extension"],
            },
        }
    )

    msg = parser.parse_line(line, 4)

    assert msg is not None
    assert msg.role == "assistant"
    assert msg.content_type == "tool_use"
    assert msg.tool_name == "WebSearch"
    assert msg.tool_input == {
        "type": "search",
        "query": "pg_search docs",
        "queries": ["pg_search docs", "postgres pg_search extension"],
        "status": "completed",
    }


def test_parse_custom_tool_call_as_tool_use() -> None:
    parser = CodexTranscriptParser()
    line = _response_item(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "call_123",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** End Patch\n",
        }
    )

    msg = parser.parse_line(line, 9)

    assert msg is not None
    assert msg.content_type == "tool_use"
    assert msg.tool_name == "apply_patch"
    assert msg.tool_use_id == "call_123"
    assert msg.tool_input == {
        "raw": "*** Begin Patch\n*** End Patch\n",
        "status": "completed",
    }


def test_parse_custom_tool_call_output_decodes_json_string_payload() -> None:
    parser = CodexTranscriptParser()
    line = _response_item(
        {
            "type": "custom_tool_call_output",
            "call_id": "call_123",
            "output": json.dumps(
                {
                    "output": "Success. Updated the following files:\nM src/gobby/foo.py\n",
                    "metadata": {"exit_code": 0},
                }
            ),
        }
    )

    msg = parser.parse_line(line, 10)

    assert msg is not None
    assert msg.role == "tool"
    assert msg.content_type == "tool_result"
    assert msg.tool_use_id == "call_123"
    assert msg.content == "Success. Updated the following files:\nM src/gobby/foo.py\n"
    assert msg.tool_result == {
        "output": "Success. Updated the following files:\nM src/gobby/foo.py\n",
        "metadata": {"exit_code": 0},
    }
