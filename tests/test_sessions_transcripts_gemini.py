"""
Tests for GeminiTranscriptParser.
"""

import json

from gobby.sessions.transcripts.gemini import GeminiTranscriptParser


def test_gemini_parser_generic_message():
    parser = GeminiTranscriptParser()

    # Test simple user message
    line = json.dumps(
        {"role": "user", "content": "Hello world", "timestamp": "2023-01-01T12:00:00Z"}
    )

    msg = parser.parse_line(line, 0)
    assert msg is not None
    assert msg.role == "user"
    assert msg.content == "Hello world"
    assert msg.index == 0
    assert msg.timestamp.year == 2023


def test_gemini_parser_model_response():
    parser = GeminiTranscriptParser()

    # Test model response
    line = json.dumps(
        {"role": "model", "content": "I am Gemini", "timestamp": "2023-01-01T12:00:01Z"}
    )

    msg = parser.parse_line(line, 1)
    assert msg is not None
    assert msg.role == "assistant"  # Normalized
    assert msg.content == "I am Gemini"


def test_gemini_parser_nested_message_structure():
    parser = GeminiTranscriptParser()

    # Test nested message structure often seen in Google APIs
    line = json.dumps(
        {
            "message": {"role": "user", "content": "Nested content"},
            "timestamp": "2023-01-01T12:00:02Z",
        }
    )

    msg = parser.parse_line(line, 2)
    assert msg is not None
    assert msg.role == "user"
    assert msg.content == "Nested content"


def test_gemini_parser_list_content():
    parser = GeminiTranscriptParser()

    # Test content as list of parts
    line = json.dumps({"role": "model", "content": [{"text": "Part 1"}, "Part 2"]})

    msg = parser.parse_line(line, 3)
    assert msg is not None
    assert "Part 1" in msg.content
    assert "Part 2" in msg.content
