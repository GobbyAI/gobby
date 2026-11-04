"""Attribution of Claude auto-compaction hooks to the parent or a subagent (#25683).

These tests drive CompactionOwnership with synthetic payloads and real transcript files: a
compact_boundary written to the parent transcript or to its subagents/ directory. They name no
bundled definition.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks import compaction_ownership
from gobby.hooks.compaction_ownership import (
    REPLAY_FLAG,
    CompactionOwnership,
    _boundaries_since,
    _boundary_records,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource

pytestmark = pytest.mark.unit

SESSION_ID = "compaction-attribution-session"
CHUNK_SIZES = [1, 7, 64, 333, 4096]


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(record: object) -> bytes:
    return json.dumps(record).encode() + b"\n"


def _boundary_line(uuid: str, at: datetime, trigger: str | None = "auto") -> bytes:
    record: dict[str, Any] = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "timestamp": _stamp(at),
    }
    if trigger is not None:
        record["compactMetadata"] = {"trigger": trigger}
    return _line(record)


def _append(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(data)


def _subagent_transcript(parent: Path) -> Path:
    return parent.with_suffix("") / "subagents" / "agent-7.jsonl"


def _event(
    event_type: HookEventType,
    transcript: Path | None,
    *,
    origin: SessionSource = SessionSource.CLAUDE,
    metadata: dict[str, Any] | None = None,
    **data: Any,
) -> HookEvent:
    payload: dict[str, Any] = {"cwd": "/tmp", **data}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=origin,
        timestamp=datetime.now(UTC),
        data=payload,
        metadata=dict(metadata or {}),
    )


def _pre(transcript: Path | None, trigger: str = "auto") -> HookEvent:
    return _event(HookEventType.PRE_COMPACT, transcript, trigger=trigger)


def _after_compact(transcript: Path | None) -> HookEvent:
    return _event(HookEventType.SESSION_START, transcript, source="compact")


def _post(transcript: Path | None, trigger: str = "auto") -> HookEvent:
    return _event(HookEventType.POST_COMPACT, transcript, trigger=trigger)


def _prompt(transcript: Path | None, prompt: str) -> HookEvent:
    return _event(HookEventType.BEFORE_AGENT, transcript, prompt=prompt)


def _outsider(kind: str, transcript: Path) -> HookEvent:
    """An auto PreCompact that is not the parent's main-thread compaction."""
    if kind == "codex":
        return _event(
            HookEventType.PRE_COMPACT, transcript, origin=SessionSource.CODEX, trigger="auto"
        )
    if kind == "subagent":
        return _event(HookEventType.PRE_COMPACT, transcript, trigger="auto", agent_id="agent-7")
    return _event(
        HookEventType.PRE_COMPACT,
        transcript,
        metadata={REPLAY_FLAG: True},
        trigger="auto",
    )


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "parent.jsonl"
    path.touch()
    return path


@pytest.fixture
def ownership() -> CompactionOwnership:
    return CompactionOwnership()


def test_manual_pre_compact_is_never_held(ownership: CompactionOwnership, transcript: Path) -> None:
    assert ownership.route(_pre(transcript, trigger="manual")) == ([], False)
    assert ownership.route(_after_compact(transcript)) == ([], False)


def test_auto_pre_compact_is_held_until_a_boundary_claims_it(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)


@pytest.mark.parametrize("source", ["startup", "clear", "resume"])
def test_session_starts_from_other_sources_are_never_held(
    ownership: CompactionOwnership, transcript: Path, source: str
) -> None:
    start = _event(HookEventType.SESSION_START, transcript, source=source)

    assert ownership.route(start) == ([], False)


def test_other_session_starts_do_not_release_a_pending_pre_compact(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    assert ownership.route(_event(HookEventType.SESSION_START, transcript, source="startup")) == (
        [],
        False,
    )

    _append(transcript, _boundary_line("boundary-1", datetime.now(UTC)))
    replays, held = ownership.route(_after_compact(transcript))

    assert [event.event_type for event in replays] == [HookEventType.PRE_COMPACT]
    assert held is False


def test_tails_without_a_compaction_in_flight_pass(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_after_compact(transcript)) == ([], False)
    assert ownership.route(_post(transcript)) == ([], False)


@pytest.mark.parametrize("kind", ["codex", "subagent", "replayed"])
def test_outsider_pre_compact_is_never_held_and_leaves_the_state_alone(
    ownership: CompactionOwnership, transcript: Path, kind: str
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)

    assert ownership.route(_outsider(kind, transcript)) == ([], False)

    _append(transcript, _boundary_line("boundary-1", datetime.now(UTC)))
    replays, held = ownership.route(_after_compact(transcript))

    assert [event.event_type for event in replays] == [HookEventType.PRE_COMPACT]
    assert held is False


def test_subagent_marked_tail_is_not_held_or_released(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    marked = _event(HookEventType.SESSION_START, transcript, source="compact", agent_id="agent-7")

    assert ownership.route(marked) == ([], False)


def test_parent_boundary_releases_the_pre_compact_on_the_session_start_tail(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    pre = _pre(transcript)
    assert ownership.route(pre) == ([], True)
    _append(transcript, _boundary_line("boundary-1", datetime.now(UTC)))

    replays, held = ownership.route(_after_compact(transcript))

    assert held is False
    assert [event.event_type for event in replays] == [HookEventType.PRE_COMPACT]
    assert replays[0].data == pre.data
    assert replays[0].metadata[REPLAY_FLAG] is True


def test_parent_post_compact_after_its_boundary_passes(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    _append(transcript, _boundary_line("boundary-1", datetime.now(UTC)))
    ownership.route(_after_compact(transcript))

    assert ownership.route(_post(transcript)) == ([], False)


def test_a_released_pre_compact_is_not_replayed_again(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    _append(transcript, _boundary_line("boundary-1", datetime.now(UTC)))
    replays, _ = ownership.route(_after_compact(transcript))
    assert len(replays) == 1

    assert ownership.route(_prompt(transcript, "continue")) == ([], False)


def test_subagent_boundary_drops_the_pre_compact_and_holds_its_tails(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    _append(_subagent_transcript(transcript), _boundary_line("subagent-1", datetime.now(UTC)))

    assert ownership.route(_after_compact(transcript)) == ([], True)
    assert ownership.route(_post(transcript)) == ([], True)

    _append(transcript, _boundary_line("parent-1", datetime.now(UTC)))
    assert ownership.route(_prompt(transcript, "continue")) == ([], False)


def test_tails_without_any_boundary_are_held(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)

    assert ownership.route(_after_compact(transcript)) == ([], True)
    assert ownership.route(_post(transcript)) == ([], True)


def test_overlapping_pre_compacts_fail_closed(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    assert ownership.route(_pre(transcript)) == ([], True)
    _append(transcript, _boundary_line("parent-1", datetime.now(UTC)))

    assert ownership.route(_after_compact(transcript)) == ([], False)
    assert ownership.route(_post(transcript)) == ([], False)
    assert ownership.route(_prompt(transcript, "continue")) == ([], False)


def test_a_parent_boundary_releases_one_compaction_only(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    _append(transcript, _boundary_line("parent-1", datetime.now(UTC)))
    replays, _ = ownership.route(_prompt(transcript, "first"))
    assert [event.event_type for event in replays] == [HookEventType.PRE_COMPACT]

    assert ownership.route(_pre(transcript)) == ([], True)

    assert ownership.route(_prompt(transcript, "second")) == ([], False)


def test_a_finished_subagent_compaction_does_not_consume_the_parent_boundary(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.route(_pre(transcript)) == ([], True)
    _append(_subagent_transcript(transcript), _boundary_line("subagent-1", datetime.now(UTC)))
    assert ownership.route(_after_compact(transcript)) == ([], True)

    assert ownership.route(_pre(transcript)) == ([], True)
    _append(transcript, _boundary_line("parent-1", datetime.now(UTC)))
    replays, held = ownership.route(_after_compact(transcript))

    assert held is False
    assert [event.event_type for event in replays] == [HookEventType.PRE_COMPACT]


def test_unreadable_transcript_never_replays(
    ownership: CompactionOwnership, tmp_path: Path
) -> None:
    missing = tmp_path / "gone.jsonl"

    assert ownership.route(_pre(missing)) == ([], True)
    assert ownership.route(_after_compact(missing)) == ([], True)
    assert ownership.route(_prompt(missing, "continue")) == ([], False)


def test_compaction_without_a_transcript_never_replays(ownership: CompactionOwnership) -> None:
    assert ownership.route(_pre(None)) == ([], True)

    assert ownership.route(_after_compact(None)) == ([], True)
    assert ownership.route(_prompt(None, "continue")) == ([], False)


def test_a_pending_pre_compact_expires_after_its_window(
    ownership: CompactionOwnership, transcript: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A negative window expires every pending PreCompact at the next event, deterministically.
    monkeypatch.setattr(compaction_ownership, "_PENDING_TTL", timedelta(seconds=-1))
    assert ownership.route(_pre(transcript)) == ([], True)
    _append(transcript, _boundary_line("parent-1", datetime.now(UTC)))

    assert ownership.route(_prompt(transcript, "continue")) == ([], False)


def test_needs_transcripts_for_pre_compact_and_tails(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    assert ownership.needs_transcripts(_pre(transcript)) is True
    assert ownership.needs_transcripts(_post(transcript)) is True
    assert ownership.needs_transcripts(_after_compact(transcript)) is True


@pytest.mark.parametrize("kind", ["codex", "subagent", "replayed"])
def test_needs_transcripts_is_false_for_outsiders(
    ownership: CompactionOwnership, transcript: Path, kind: str
) -> None:
    assert ownership.needs_transcripts(_outsider(kind, transcript)) is False


def test_needs_transcripts_is_false_for_quiet_events_until_a_pre_compact_is_pending(
    ownership: CompactionOwnership, transcript: Path
) -> None:
    start = _event(HookEventType.SESSION_START, transcript, source="startup")

    assert ownership.needs_transcripts(start) is False
    assert ownership.needs_transcripts(_prompt(transcript, "continue")) is False

    ownership.route(_pre(transcript))

    assert ownership.needs_transcripts(_prompt(transcript, "continue")) is True


@pytest.mark.parametrize("chunk_bytes", CHUNK_SIZES)
def test_boundary_scan_finds_the_same_records_whatever_the_read_size(
    tmp_path: Path, chunk_bytes: int
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "parent.jsonl"
    expected: list[str] = []
    for index in range(30):
        at = now - timedelta(seconds=290 - index * 9)
        if index % 2 == 0:
            expected.append(f"boundary-{index}")
            _append(path, _boundary_line(f"boundary-{index}", at))
        else:
            filler = {"type": "user", "timestamp": _stamp(at), "padding": "x" * (index * 13 % 97)}
            _append(path, _line(filler))

    records = _boundary_records(path, now - timedelta(minutes=5), chunk_bytes)

    assert [uuid for uuid, _at, _trigger in records] == expected


def test_boundary_scan_keeps_the_window_order_and_triggers(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "parent.jsonl"
    _append(path, _boundary_line("too-old", now - timedelta(minutes=10), "auto"))
    _append(path, _boundary_line("earlier", now - timedelta(minutes=2), "auto"))
    _append(path, _boundary_line("manual", now - timedelta(minutes=1), "manual"))
    _append(path, _boundary_line("untriggered", now - timedelta(seconds=30), None))

    records = _boundary_records(path, now - timedelta(minutes=5), 4096)

    assert [(uuid, trigger) for uuid, _at, trigger in records] == [
        ("earlier", "auto"),
        ("manual", "manual"),
        ("untriggered", None),
    ]


def test_malformed_and_incomplete_records_are_skipped(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "parent.jsonl"
    _append(path, b"not json\n")
    _append(path, b"[1, 2]\n")
    _append(path, b'"text"\n')
    _append(path, b"\n")
    _append(path, _line({"subtype": "compact_boundary", "uuid": "no-time"}))
    _append(path, _line({"subtype": "compact_boundary", "uuid": 5, "timestamp": _stamp(now)}))
    _append(path, _line({"subtype": "compact_boundary", "uuid": "bad", "timestamp": "yesterday"}))
    _append(path, _boundary_line("valid", now - timedelta(seconds=1)))

    records = _boundary_records(path, now - timedelta(minutes=5), 4096)

    assert [uuid for uuid, _at, _trigger in records] == ["valid"]


def test_a_last_record_without_a_newline_is_read(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "parent.jsonl"
    _append(path, _boundary_line("first", now - timedelta(seconds=5)))
    record = {"subtype": "compact_boundary", "uuid": "last", "timestamp": _stamp(now)}
    _append(path, json.dumps(record).encode())

    records = _boundary_records(path, now - timedelta(minutes=1), 7)

    assert [uuid for uuid, _at, _trigger in records] == ["first", "last"]


def test_a_subagent_transcript_last_written_before_the_window_is_not_read(
    transcript: Path,
) -> None:
    now = datetime.now(UTC)
    subagent = _subagent_transcript(transcript)
    _append(subagent, _boundary_line("subagent-old", now - timedelta(seconds=30)))
    stale = (now - timedelta(minutes=10)).timestamp()
    os.utime(subagent, (stale, stale))

    assert _boundaries_since(transcript, now - timedelta(minutes=1)) == []
