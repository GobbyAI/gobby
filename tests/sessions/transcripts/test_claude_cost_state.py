"""Cumulative Claude cost snapshots must not become messages or usage deltas."""

import json

from pytest_mock import MockerFixture

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

COST_STATE = {
    "type": "cost-state",
    "sessionId": "cost-state-regression",
    "totalCostUSD": 0.75,
    "modelUsage": {
        "claude-test": {
            "inputTokens": 100,
            "outputTokens": 200,
            "cacheReadInputTokens": 300,
            "cacheCreationInputTokens": 400,
            "costUSD": 0.75,
        }
    },
}


def test_cost_state_is_recognized_without_parser_diagnostics(mocker: MockerFixture) -> None:
    parser = ClaudeTranscriptParser(session_id="cost-state-regression")
    unknown = mocker.patch.object(parser.error_log, "log_unknown_block")

    assert parser.parse_line(json.dumps(COST_STATE), 0) is None
    unknown.assert_not_called()


def test_cost_snapshots_do_not_duplicate_streamed_message_usage(mocker: MockerFixture) -> None:
    parser = ClaudeTranscriptParser(session_id="cost-state-regression")
    unknown = mocker.patch.object(parser.error_log, "log_unknown_block")
    assistant = {
        "type": "assistant",
        "uuid": "message-1",
        "timestamp": "2026-09-05T12:00:00Z",
        "message": {
            "id": "message-1",
            "model": "claude-test",
            "content": [{"type": "text", "text": "Ready."}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }

    messages = parser.parse_lines(
        [json.dumps(COST_STATE), json.dumps(assistant), json.dumps(COST_STATE)]
    )

    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, ParsedMessage)
    assert message.role == "assistant"
    assert message.content == "Ready."
    assert message.model == "claude-test"
    assert message.index == 0
    assert message.usage is not None
    assert message.usage.input_tokens == 10
    assert message.usage.output_tokens == 20
    assert message.raw_json == assistant
    unknown.assert_not_called()
