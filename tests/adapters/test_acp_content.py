from __future__ import annotations

import pytest

from gobby.adapters.acp_content import (
    extract_text,
    normalize_prompt_blocks,
    normalize_tool_call_update,
    parse_prompt_capabilities,
)

pytestmark = pytest.mark.unit


def test_normalize_prompt_blocks_gates_optional_content() -> None:
    capabilities = {
        "promptCapabilities": {
            "image": {},
            "audio": None,
            "embeddedContext": False,
        }
    }

    blocks = normalize_prompt_blocks(
        [
            {"type": "text", "content": "hello"},
            {
                "type": "image",
                "source": {"data": "abc", "media_type": "image/png"},
            },
            {"type": "audio", "data": "def", "mimeType": "audio/wav"},
            {
                "type": "resource",
                "resource": {"uri": "file:///notes.txt", "text": "notes"},
            },
            {"type": "resource_link", "uri": "file:///linked.txt", "name": "linked.txt"},
        ],
        agent_capabilities=capabilities,
        prefix_text="context",
    )

    assert parse_prompt_capabilities(capabilities) == {
        "image": True,
        "audio": False,
        "embeddedContext": False,
    }
    assert blocks[0] == {"type": "text", "text": "context"}
    assert blocks[1] == {"type": "text", "text": "hello"}
    assert blocks[2] == {"type": "image", "data": "abc", "mimeType": "image/png"}
    assert blocks[3]["type"] == "text"
    assert "Unsupported audio content omitted" in blocks[3]["text"]
    assert blocks[4] == {
        "type": "text",
        "text": "Attached resource file:///notes.txt:\nnotes",
    }
    assert blocks[5] == {
        "type": "resource_link",
        "uri": "file:///linked.txt",
        "name": "linked.txt",
    }


def test_normalize_prompt_blocks_reads_audio_from_nested_source() -> None:
    blocks = normalize_prompt_blocks(
        [
            {
                "type": "audio",
                "source": {
                    "data": "abc123",
                    "media_type": "audio/mpeg",
                },
            }
        ],
        agent_capabilities={"promptCapabilities": {"audio": {}}},
    )

    assert blocks == [{"type": "audio", "data": "abc123", "mimeType": "audio/mpeg"}]


def test_extract_text_recurses_content_wrappers() -> None:
    assert (
        extract_text(
            {
                "type": "content",
                "content": [
                    {"type": "text", "text": "outer"},
                    {
                        "type": "content",
                        "content": {"type": "text", "content": "inner"},
                    },
                ],
            }
        )
        == "outer\ninner"
    )


def test_normalize_prompt_blocks_preserves_supported_embedded_resource() -> None:
    blocks = normalize_prompt_blocks(
        [
            {
                "type": "resource",
                "resource": {"uri": "file:///notes.txt", "text": "notes"},
            }
        ],
        agent_capabilities={"promptCapabilities": {"embeddedContext": True}},
    )

    assert blocks == [
        {
            "type": "resource",
            "resource": {"uri": "file:///notes.txt", "text": "notes"},
        }
    ]


def test_normalize_tool_call_update_preserves_rich_content() -> None:
    data = normalize_tool_call_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tool-1",
            "name": "canonical_edit",
            "title": "Edit",
            "kind": "edit",
            "status": "in_progress",
            "locations": [{"uri": "file:///src/app.py", "line": 12}],
            "content": [
                {
                    "type": "diff",
                    "path": "src/app.py",
                    "oldText": "old",
                    "newText": "new",
                },
                {"type": "terminal", "terminalId": "term-1"},
                {
                    "type": "content",
                    "content": {
                        "type": "resource_link",
                        "uri": "file:///src/app.py",
                        "name": "src/app.py",
                    },
                },
            ],
            "rawOutput": {"stdout": "ok"},
        }
    )

    assert data["call_id"] == "tool-1"
    assert data["tool_name"] == "canonical_edit"
    assert data["tool_status"] == "calling"
    assert data["tool_kind"] == "edit"
    assert data["locations"] == [{"uri": "file:///src/app.py", "line": 12}]
    assert data["raw_output"] == {"stdout": "ok"}
    assert [block["type"] for block in data["content_blocks"]] == [
        "diff",
        "terminal",
        "resource_link",
    ]
