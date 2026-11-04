"""Hold and settle ambiguous Claude auto-compaction hooks (#25683).

A Claude subagent shares its parent's Gobby session. Its auto-compaction hooks carry the
parent's session id and transcript path with no agent_id and no subagents/ path, so they
can only be attributed by the parent transcript: a new parent boundary means the parent
compacted; none means a subagent did.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.compaction_ownership import (
    REPLAY_FLAG,
    CompactionOwnership,
    newest_compact_boundary_since,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource

pytestmark = pytest.mark.unit

SESSION_ID = "18b6d896-29ef-483f-86f3-110271af4374"
SUBAGENT_TRANSCRIPT = (
    "/home/u/.claude/projects/p/18b6d896-29ef-483f-86f3-110271af4374/subagents/agent-adfb45.jsonl"
)


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _user_line(at: datetime, uuid: str, *, padding: int = 0) -> bytes:
    record: dict[str, Any] = {
        "type": "user",
        "uuid": uuid,
        "timestamp": _stamp(at),
        "message": {"role": "user", "content": "x" * padding},
    }
    return json.dumps(record).encode() + b"\n"


def _boundary_line(at: datetime, uuid: str, *, padding: int = 0) -> bytes:
    record: dict[str, Any] = {
        "type": "system",
        "subtype": "compact_boundary",
        "content": "Conversation compacted",
        "uuid": uuid,
        "timestamp": _stamp(at),
        "compactMetadata": {"trigger": "auto", "summary": "y" * padding},
    }
    return json.dumps(record).encode() + b"\n"


def _event(
    event_type: HookEventType,
    data: dict[str, Any],
    *,
    transcript: str,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data={"cwd": "/tmp", "transcript_path": transcript, **data},
    )


def _pre_compact(trigger: str, *, transcript: str, **extra: Any) -> HookEvent:
    return _event(HookEventType.PRE_COMPACT, {"trigger": trigger, **extra}, transcript=transcript)


def _session_start(source: str, *, transcript: str) -> HookEvent:
    return _event(HookEventType.SESSION_START, {"source": source}, transcript=transcript)


def _main_thread(transcript: str) -> HookEvent:
    return _event(HookEventType.BEFORE_AGENT, {"prompt": "continue"}, transcript=transcript)


def _append(path: Path, line: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(line)


@pytest.fixture
def parent_transcript(tmp_path: Path) -> Path:
    path = tmp_path / "parent.jsonl"
    path.write_bytes(_user_line(datetime.now(UTC) - timedelta(minutes=3), "u-0"))
    return path


@pytest.fixture
def ownership() -> CompactionOwnership:
    return CompactionOwnership()


# --- newest_compact_boundary_since: the transcript attribution probe ---


def test_finds_boundary_written_inside_the_window(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        _user_line(now - timedelta(seconds=30), "u-1")
        + _boundary_line(now, "boundary-1")
        + _user_line(now + timedelta(seconds=1), "u-2")
    )

    assert newest_compact_boundary_since(path, now - timedelta(seconds=5)) == "boundary-1"


def test_newest_boundary_wins(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        _boundary_line(now - timedelta(seconds=2), "boundary-old")
        + _user_line(now - timedelta(seconds=1), "u-1")
        + _boundary_line(now, "boundary-new")
    )

    assert newest_compact_boundary_since(path, now - timedelta(seconds=5)) == "boundary-new"


def test_boundary_older_than_the_window_is_not_found(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        _boundary_line(now - timedelta(minutes=1), "boundary-old")
        + _user_line(now - timedelta(seconds=2), "u-1")
    )

    assert newest_compact_boundary_since(path, now - timedelta(seconds=5)) is None


def test_transcript_without_boundary_yields_none(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "t.jsonl"
    path.write_bytes(_user_line(now, "u-1") + _user_line(now, "u-2"))

    assert newest_compact_boundary_since(path, now - timedelta(seconds=5)) is None


def test_missing_transcript_yields_none(tmp_path: Path) -> None:
    assert newest_compact_boundary_since(tmp_path / "missing.jsonl", datetime.now(UTC)) is None


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"truncated": \n' + _boundary_line(now, "boundary-1") + b"not json\n")

    assert newest_compact_boundary_since(path, now - timedelta(seconds=5)) == "boundary-1"


@pytest.mark.parametrize("chunk_bytes", [1, 7, 64, 333, 4096])
def test_boundary_is_found_across_every_chunk_split(tmp_path: Path, chunk_bytes: int) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        _user_line(now - timedelta(seconds=4), "u-1", padding=150)
        + _boundary_line(now, "boundary-1", padding=90)
        + _user_line(now + timedelta(seconds=1), "u-2", padding=40)
    )

    found = newest_compact_boundary_since(path, now - timedelta(seconds=5), chunk_bytes=chunk_bytes)

    assert found == "boundary-1"


def test_lines_longer_than_a_chunk_are_read_whole(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        _boundary_line(now, "boundary-1", padding=10_000)
        + _user_line(now + timedelta(seconds=1), "u-2", padding=10_000)
    )

    found = newest_compact_boundary_since(path, now - timedelta(seconds=5), chunk_bytes=512)

    assert found == "boundary-1"


# --- hold: which compaction hooks wait for an owner ---


def test_auto_pre_compact_is_held(ownership: CompactionOwnership, parent_transcript: Path) -> None:
    pre = _pre_compact("auto", transcript=str(parent_transcript))

    assert ownership.hold(pre) is True


def test_manual_pre_compact_is_never_held(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    pre = _pre_compact("manual", transcript=str(parent_transcript))

    assert ownership.hold(pre) is False


def test_session_start_compact_is_held_only_after_an_auto_pre_compact(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    assert ownership.hold(_session_start("compact", transcript=transcript)) is False

    assert ownership.hold(_pre_compact("auto", transcript=transcript)) is True
    assert ownership.hold(_session_start("compact", transcript=transcript)) is True


def test_session_start_after_manual_pre_compact_is_not_held(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    ownership.hold(_pre_compact("auto", transcript=transcript))
    ownership.hold(_pre_compact("manual", transcript=transcript))

    assert ownership.hold(_session_start("compact", transcript=transcript)) is False


@pytest.mark.parametrize("source", ["clear", "startup", "resume"])
def test_session_start_without_compact_source_is_never_held(
    ownership: CompactionOwnership, parent_transcript: Path, source: str
) -> None:
    transcript = str(parent_transcript)
    ownership.hold(_pre_compact("auto", transcript=transcript))

    assert ownership.hold(_session_start(source, transcript=transcript)) is False


def test_post_compact_is_held_only_after_an_auto_pre_compact(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    post = _event(HookEventType.POST_COMPACT, {"trigger": "auto"}, transcript=transcript)
    assert ownership.hold(post) is False

    ownership.hold(_pre_compact("auto", transcript=transcript))
    assert ownership.hold(post) is True

    ownership.hold(_pre_compact("manual", transcript=transcript))
    assert ownership.hold(post) is False


def test_non_compaction_events_are_never_held(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    ownership.hold(_pre_compact("auto", transcript=transcript))

    assert ownership.hold(_main_thread(transcript)) is False


def test_non_claude_compaction_is_never_held(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    codex = _event(
        HookEventType.PRE_COMPACT,
        {"trigger": "auto"},
        transcript=str(parent_transcript),
        source=SessionSource.CODEX,
    )

    assert ownership.hold(codex) is False


def test_subagent_marked_compaction_is_not_held_and_leaves_the_trigger_alone(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    by_agent_id = _pre_compact("auto", transcript=transcript, agent_id="adfb45")
    by_path = _pre_compact("auto", transcript=SUBAGENT_TRANSCRIPT)

    assert ownership.hold(by_agent_id) is False
    assert ownership.hold(by_path) is False
    assert ownership.hold(_session_start("compact", transcript=transcript)) is False


def test_replayed_event_is_never_held_again(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    replay = _pre_compact("auto", transcript=str(parent_transcript))
    replay.metadata[REPLAY_FLAG] = True

    assert ownership.hold(replay) is False


# --- settle: attribute the held batch on the parent's next main-thread event ---


def test_settle_replays_held_events_when_the_parent_recorded_a_boundary(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    pre = _pre_compact("auto", transcript=transcript)
    start = _session_start("compact", transcript=transcript)
    ownership.hold(pre)
    ownership.hold(start)
    _append(parent_transcript, _boundary_line(datetime.now(UTC), "boundary-1"))

    replayed = ownership.settle(_main_thread(transcript))

    assert replayed == [pre, start]
    assert all(event.metadata.get(REPLAY_FLAG) is True for event in replayed)


def test_settle_drops_held_events_when_only_a_subagent_compacted(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    ownership.hold(_pre_compact("auto", transcript=transcript))
    ownership.hold(_session_start("compact", transcript=transcript))

    assert ownership.settle(_main_thread(transcript)) == []
    assert ownership.settle(_main_thread(transcript)) == []


def test_settle_never_replays_a_boundary_it_already_verified(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    _append(parent_transcript, _boundary_line(datetime.now(UTC), "boundary-1"))
    ownership.hold(_pre_compact("auto", transcript=transcript))
    assert len(ownership.settle(_main_thread(transcript))) == 1

    ownership.hold(_session_start("compact", transcript=transcript))

    assert ownership.settle(_main_thread(transcript)) == []


def test_settle_ignores_subagent_events_until_a_main_thread_event(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    transcript = str(parent_transcript)
    pre = _pre_compact("auto", transcript=transcript)
    ownership.hold(pre)
    _append(parent_transcript, _boundary_line(datetime.now(UTC), "boundary-1"))
    subagent_tool = _event(
        HookEventType.BEFORE_AGENT, {"agent_id": "adfb45"}, transcript=transcript
    )

    assert ownership.settle(subagent_tool) == []
    assert ownership.settle(_main_thread(transcript)) == [pre]


def test_settle_drops_when_the_parent_transcript_is_unreadable(
    ownership: CompactionOwnership, tmp_path: Path
) -> None:
    gone = str(tmp_path / "gone.jsonl")
    ownership.hold(_pre_compact("auto", transcript=gone))

    assert ownership.settle(_main_thread(gone)) == []


def test_settle_with_nothing_held_is_a_no_op(
    ownership: CompactionOwnership, parent_transcript: Path
) -> None:
    assert ownership.settle(_main_thread(str(parent_transcript))) == []
