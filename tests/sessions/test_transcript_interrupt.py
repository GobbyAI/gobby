"""Per-turn interrupt detection from bounded provider transcript tails."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import SessionSource
from gobby.sessions.transcript_interrupt import turn_interrupt_initiated

pytestmark = pytest.mark.unit


def _claude_user(content: Any, *, rejected: bool = False) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": content},
    }
    if rejected:
        record["toolDenialKind"] = "user-rejected"
    return record


def _claude_assistant(text: str = "done") -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def test_claude_prompt_after_interrupt_marks_turn_interrupt_initiated(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_records(
        transcript,
        [
            _claude_user("[Request interrupted by user]"),
            _claude_user([{"type": "text", "text": "hey had a question"}]),
            _claude_assistant("go ahead"),
        ],
    )

    assert turn_interrupt_initiated(SessionSource.CLAUDE, transcript) is True


@pytest.mark.parametrize(
    "records",
    [
        [_claude_user("[Request interrupted by user]")],
        [_claude_user([{"type": "text", "text": "[Request interrupted by user for tool use]"}])],
        [
            _claude_user(
                [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "rejected"}],
                rejected=True,
            ),
            _claude_assistant("What should I do instead?"),
        ],
    ],
    ids=["bare-string", "tool-use-variant", "user-rejected-tool"],
)
def test_claude_yield_directly_after_interrupt_or_user_rejected_tool_is_interrupt_initiated(
    tmp_path: Path,
    records: list[dict[str, Any]],
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_records(transcript, records)

    assert turn_interrupt_initiated("claude", transcript) is True


def test_claude_plain_prompt_and_stop_hook_feedback_continuation_are_not_interrupt_initiated(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_records(transcript, [_claude_user("ordinary prompt"), _claude_assistant()])

    assert turn_interrupt_initiated("claude", transcript) is False

    _write_records(
        transcript,
        [
            _claude_assistant("original response"),
            _claude_user("Stop hook feedback: close the task"),
            _claude_assistant("continuing after the block"),
        ],
    )

    assert turn_interrupt_initiated("claude", transcript) is False


def test_codex_turn_aborted_before_prompt_is_interrupt_initiated(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_records(
        transcript,
        [
            {"type": "event_msg", "payload": {"type": "turn_aborted"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": []},
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": []},
            },
        ],
    )

    assert turn_interrupt_initiated(SessionSource.CODEX, transcript) is True

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")

    assert turn_interrupt_initiated(SessionSource.CODEX, transcript) is False


def test_unknown_cli_or_missing_transcript_is_false(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_records(transcript, [_claude_user("[Request interrupted by user]")])

    assert turn_interrupt_initiated(SessionSource.DROID, transcript) is False
    assert turn_interrupt_initiated(SessionSource.CLAUDE, tmp_path / "missing.jsonl") is False
    assert turn_interrupt_initiated(SessionSource.CLAUDE, None) is False


def test_detector_reads_only_bounded_tail_and_skips_malformed_lines(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    historical_interrupt = f"{json.dumps(_claude_user('[Request interrupted by user]'))}\n"
    ordinary_turn = "".join(
        f"{json.dumps(record)}\n"
        for record in (_claude_user("ordinary prompt"), _claude_assistant())
    )
    transcript.write_bytes(
        historical_interrupt.encode() + (b"x" * (256 * 1024 + 512)) + b"\n" + ordinary_turn.encode()
    )

    assert turn_interrupt_initiated(SessionSource.CLAUDE, transcript) is False
