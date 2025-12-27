"""
Tests for CodexTranscriptParser.
"""

import json

from gobby.sessions.transcripts.codex import CodexTranscriptParser


def test_codex_parser_simple():
    parser = CodexTranscriptParser()

    line = json.dumps(
        {"role": "user", "content": "def hello():", "timestamp": "2023-01-01T12:00:00Z"}
    )

    msg = parser.parse_line(line, 0)
    assert msg is not None
    assert msg.role == "user"
    assert msg.content == "def hello():"
    assert msg.index == 0


def test_codex_parser_missing_role():
    parser = CodexTranscriptParser()
    line = json.dumps({"content": "missing role"})
    msg = parser.parse_line(line, 0)
    assert msg is None
