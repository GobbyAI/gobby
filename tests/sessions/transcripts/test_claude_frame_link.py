"""Claude artifact bookkeeping stays outside the conversation and error log."""

import json

import pytest
from pytest_mock import MockerFixture

from gobby.sessions.transcripts.base import ParsedMessage, raw_lines_from_texts
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

pytestmark = pytest.mark.unit

FRAME_LINKS: list[dict[str, object]] = [
    {
        "type": "frame-link",
        "sessionId": "frame-link-regression",
        "timestamp": "2026-09-29T23:00:00Z",
        "artifactCount": 2,
    },
    {
        "type": "frame-link",
        "sessionId": "frame-link-regression",
        "timestamp": "2026-09-29T23:00:00Z",
        "artifactCount": 2,
        "frameUrl": "https://example.invalid/frame",
        "path": "/tmp/example-artifact.html",
        "title": "Example artifact",
    },
]


@pytest.fixture
def parser(mocker: MockerFixture) -> ClaudeTranscriptParser:
    mocker.patch("gobby.sessions.transcripts.base.get_parser_error_logger")
    return ClaudeTranscriptParser(session_id="frame-link-regression")


@pytest.mark.parametrize("frame_link", FRAME_LINKS, ids=["count", "link"])
def test_frame_link_is_known_metadata(
    parser: ClaudeTranscriptParser, frame_link: dict[str, object], mocker: MockerFixture
) -> None:
    unknown = mocker.spy(parser.error_log, "log_unknown_block")

    assert parser.parse_line(json.dumps(frame_link), 0) is None
    unknown.assert_not_called()


@pytest.mark.parametrize("frame_link", FRAME_LINKS, ids=["count", "link"])
def test_frame_link_preserves_streamed_conversation_order(
    parser: ClaudeTranscriptParser, frame_link: dict[str, object], mocker: MockerFixture
) -> None:
    unknown = mocker.spy(parser.error_log, "log_unknown_block")
    before = {"type": "user", "message": {"content": "Create an artifact."}}
    after = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Artifact ready."}]},
    }
    lines = [json.dumps(record) for record in (before, frame_link, after)]

    events = list(parser.iter_parse_events(raw_lines_from_texts(lines), start_index=7))

    assert len(events) == 2
    messages: list[ParsedMessage] = []
    for event in events:
        for record in event.records:
            assert isinstance(record, ParsedMessage)
            messages.append(record)
    assert [message.content for message in messages] == [
        "Create an artifact.",
        "Artifact ready.",
    ]
    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.index for message in messages] == [7, 8]
    assert [message.raw_json for message in messages] == [before, after]
    assert [event.raw_line_no for event in events] == [0, 2]
    unknown.assert_not_called()
