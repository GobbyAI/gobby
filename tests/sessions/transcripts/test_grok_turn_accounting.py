"""Accounting suite over the four audited Grok stream shapes."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, cast

import pytest

from gobby.memory.synthetic_prompts import synthetic_body_reason
from gobby.sessions.message_stats import MessageProtocol, compute_message_stats
from gobby.sessions.transcripts.base import (
    NON_MESSAGE_CONTENT_TYPES,
    ParsedMessage,
    TokenUsage,
)
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from tests.sessions.transcripts.fixtures.grok_streams import (
    SESSION_10695_PAIR_COUNT,
    SESSION_10695_TURN_COUNT,
    SESSION_10695_TURNS,
    SESSION_10695_USAGE,
    SESSION_10711_PAIR_COUNT,
    SESSION_10711_TURN_COUNT,
    SESSION_10711_TURNS,
    SESSION_10711_USAGE,
    SESSION_10715_PAIR_COUNT,
    SESSION_10715_TURN_COUNT,
    SESSION_10715_TURNS,
    SESSION_10715_USAGE,
    SESSION_10725_PAIR_COUNT,
    SESSION_10725_TURN_COUNT,
    SESSION_10725_TURNS,
    SESSION_10725_USAGE,
    TurnSpec,
    session_10695_shape,
    session_10711_shape,
    session_10715_shape,
    session_10725_shape,
)

pytestmark = pytest.mark.unit

_KNOWN_CONTENT_TYPES = frozenset({"text", "thinking", "tool_use", "tool_result", "turn_completed"})
_SHAPE_CASES: tuple[
    tuple[str, Callable[[], list[str]], tuple[TurnSpec, ...], int, int, TokenUsage],
    ...,
] = (
    (
        "10695",
        session_10695_shape,
        SESSION_10695_TURNS,
        SESSION_10695_TURN_COUNT,
        SESSION_10695_PAIR_COUNT,
        SESSION_10695_USAGE,
    ),
    (
        "10715",
        session_10715_shape,
        SESSION_10715_TURNS,
        SESSION_10715_TURN_COUNT,
        SESSION_10715_PAIR_COUNT,
        SESSION_10715_USAGE,
    ),
    (
        "10725",
        session_10725_shape,
        SESSION_10725_TURNS,
        SESSION_10725_TURN_COUNT,
        SESSION_10725_PAIR_COUNT,
        SESSION_10725_USAGE,
    ),
    (
        "10711",
        session_10711_shape,
        SESSION_10711_TURNS,
        SESSION_10711_TURN_COUNT,
        SESSION_10711_PAIR_COUNT,
        SESSION_10711_USAGE,
    ),
)


@pytest.mark.parametrize(
    ("name", "builder", "turns", "turn_count", "pair_count", "expected_usage"),
    _SHAPE_CASES,
    ids=[case[0] for case in _SHAPE_CASES],
)
def test_audited_shapes_turn_and_usage_accounting(
    name: str,
    builder: Callable[[], list[str]],
    turns: tuple[TurnSpec, ...],
    turn_count: int,
    pair_count: int,
    expected_usage: TokenUsage,
) -> None:
    parser = GrokTranscriptParser(session_id="grok-audit")
    lines = builder()
    assert lines, f"{name}: empty stream"
    records = [_assert_real_envelope(line) for line in lines]
    _assert_shape_counts(name, records, turns)

    parsed = parser.parse_lines(lines)
    messages = [item for item in parsed if isinstance(item, ParsedMessage)]
    assert messages, f"{name}: parser returned no messages"
    sentinels = [item for item in messages if _is_unknown_sentinel(item)]
    assert sentinels == [], f"{name}: unknown-block sentinels {sentinels!r}"

    stats = compute_message_stats(cast(Sequence[MessageProtocol], messages))
    boundaries = [item for item in messages if item.content_type == "turn_completed"]
    assert len(boundaries) == turn_count, name
    assert stats["turn_count"] == turn_count, name
    assert stats["message_count"] == len(messages) - len(boundaries), name
    assert all(item.content_type in NON_MESSAGE_CONTENT_TYPES for item in boundaries)

    assert _sum_usage(messages) == expected_usage, name

    extracted = parser.extract_last_messages(records, num_pairs=max(1, len(records)))
    assert _adjacency_pair_count(extracted) == pair_count, name


def test_10695_audit_counts() -> None:
    _assert_10695_audit_counts()


def test_10715_marathon_shape() -> None:
    _assert_10715_marathon_shape()


def test_10725_all_synthetic() -> None:
    _assert_10725_all_synthetic()


def test_10711_real_prompt_count() -> None:
    _assert_10711_real_prompt_count()


def _assert_real_envelope(line: str) -> dict[str, Any]:
    record = json.loads(line)
    assert isinstance(record, dict)
    assert record["method"] in {"session/update", "_x.ai/session/update"}
    params = record["params"]
    assert isinstance(params, dict)
    assert isinstance(params["sessionId"], str) and params["sessionId"]
    update = params["update"]
    assert isinstance(update, dict)
    assert isinstance(update.get("sessionUpdate"), str)
    return record


def _assert_shape_counts(
    name: str, records: list[dict[str, Any]], turns: tuple[TurnSpec, ...]
) -> None:
    types = [_update_type(record) for record in records]
    assert types.count("turn_completed") == len(turns), name
    assert types.count("compaction_checkpoint") == sum(
        1 for spec in turns if spec.compaction_restart
    ), name
    assert types.count("auto_compact_completed") == types.count("compaction_checkpoint"), name
    assert types.count("agent_message_chunk") == sum(len(spec.agent_blocks) for spec in turns), name
    expected_tool_calls = sum(spec.tool_calls + len(spec.tools) for spec in turns)
    assert types.count("tool_call") == expected_tool_calls, name
    assert types.count("tool_call_update") == expected_tool_calls, name
    stop_reasons = [
        _update(record).get("stop_reason")
        for record in records
        if _update_type(record) == "turn_completed"
    ]
    assert stop_reasons.count("end_turn") == sum(
        1 for spec in turns if spec.stop_reason == "end_turn"
    ), name
    assert stop_reasons.count("cancelled") == sum(
        1 for spec in turns if spec.stop_reason == "cancelled"
    ), name


def _assert_10695_audit_counts() -> None:
    records = [_assert_real_envelope(line) for line in session_10695_shape()]
    types = [_update_type(record) for record in records]
    assert types.count("turn_completed") == 55
    assert types.count("compaction_checkpoint") == 17
    assert types.count("agent_message_chunk") == 285
    stops = [
        _update(record).get("stop_reason")
        for record in records
        if _update_type(record) == "turn_completed"
    ]
    assert stops.count("end_turn") == 35
    assert stops.count("cancelled") == 20


def _assert_10715_marathon_shape() -> None:
    records = [_assert_real_envelope(line) for line in session_10715_shape()]
    types = [_update_type(record) for record in records]
    assert types.count("turn_completed") == 4
    assert types.count("agent_message_chunk") == 53  # 50 marathon + 3 small
    users = [
        _text(_update(record)) for record in records if _update_type(record) == "user_message_chunk"
    ]
    injections = [text for text in users if text.startswith("The user sent a message")]
    assert len(injections) == 2
    marathon_usage = next(
        _update(record)["usage"]
        for record in records
        if _update_type(record) == "turn_completed"
        and "usage" in _update(record)
        and _update(record)["usage"].get("modelCalls") == 459
    )
    assert marathon_usage["modelCalls"] == 459
    local_command = [
        record
        for record in records
        if _update_type(record) == "turn_completed" and "usage" not in _update(record)
    ]
    assert len(local_command) == 1


def _assert_10725_all_synthetic() -> None:
    records = [_assert_real_envelope(line) for line in session_10725_shape()]
    prompts = [
        _text(_update(record)) for record in records if _update_type(record) == "user_message_chunk"
    ]
    assert prompts
    assert all(synthetic_body_reason(prompt) is not None for prompt in prompts)
    assert synthetic_body_reason(prompts[0]) == "daemon_wake_prompt"
    assert all(synthetic_body_reason(prompt) == "wait_directive" for prompt in prompts[1:])


def _assert_10711_real_prompt_count() -> None:
    records = [_assert_real_envelope(line) for line in session_10711_shape()]
    prompts = [
        _text(_update(record)) for record in records if _update_type(record) == "user_message_chunk"
    ]
    real = [prompt for prompt in prompts if synthetic_body_reason(prompt) is None]
    assert len(real) == 5


def _is_unknown_sentinel(message: ParsedMessage) -> bool:
    if message.content_type not in _KNOWN_CONTENT_TYPES:
        return True
    return isinstance(message.content, str) and message.content.startswith("[unsupported block:")


def _sum_usage(messages: list[ParsedMessage]) -> TokenUsage:
    total = TokenUsage()
    for message in messages:
        if message.usage is None:
            continue
        total.input_tokens += message.usage.input_tokens
        total.output_tokens += message.usage.output_tokens
        total.cache_creation_tokens += message.usage.cache_creation_tokens
        total.cache_read_tokens += message.usage.cache_read_tokens
    return total


def _adjacency_pair_count(messages: list[dict[str, Any]]) -> int:
    pairs = 0
    pending_user = False
    for message in messages:
        if message["role"] == "user":
            pending_user = True
        elif message["role"] == "assistant":
            pairs += 1
            pending_user = False
    if pending_user:
        pairs += 1
    return pairs


def _update(record: dict[str, Any]) -> dict[str, Any]:
    params = record["params"]
    assert isinstance(params, dict)
    update = params["update"]
    assert isinstance(update, dict)
    return update


def _update_type(record: dict[str, Any]) -> str:
    return str(_update(record).get("sessionUpdate") or "")


def _text(update: dict[str, Any]) -> str:
    content = update.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return str(content["text"])
    return ""
