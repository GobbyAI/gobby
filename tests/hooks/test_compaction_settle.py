"""Held-until-settle compaction ownership, unit level (#25683).

Parent transcripts are real files. The rule engine is covered by test_compaction_ownership_pipeline.py.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks import compaction_ownership
from gobby.hooks.compaction_ownership import (
    REPLAY_FLAG,
    CompactionOwnership,
    merge_replayed_context,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit

SESSION_ID = "session-under-test"
PRE = HookEventType.PRE_COMPACT
START = HookEventType.SESSION_START
POST = HookEventType.POST_COMPACT
BEFORE = HookEventType.BEFORE_AGENT


def _record(**fields: Any) -> bytes:
    return json.dumps(fields).encode() + b"\n"


def _boundary(uuid: str, **fields: Any) -> bytes:
    return _record(type="system", subtype="compact_boundary", uuid=uuid, **fields)


def _event(event_type: HookEventType, transcript: Path | None, **data: Any) -> HookEvent:
    payload = dict(data)
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=payload,
    )


@pytest.fixture
def parent(tmp_path: Path) -> Path:
    path = tmp_path / "parent.jsonl"
    path.write_bytes(_record(type="user", uuid="u-0"))
    return path


def _append(path: Path, line: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(line)


def test_auto_pre_compact_is_held_and_manual_is_not(parent: Path) -> None:
    assert CompactionOwnership().route(_event(PRE, parent, trigger="auto")) == ([], True)
    assert CompactionOwnership().route(_event(PRE, parent, trigger="manual")) == ([], False)


def test_subagent_compaction_hooks_are_not_held(parent: Path) -> None:
    ownership = CompactionOwnership()
    subagent_file = parent.with_suffix("") / "subagents" / "agent-1.jsonl"

    assert ownership.route(_event(PRE, parent, trigger="auto", agent_id="agent-1")) == ([], False)
    assert ownership.route(_event(START, subagent_file, source="compact")) == ([], False)
    assert ownership.needs_transcripts(_event(START, subagent_file, source="compact")) is False


def test_settle_before_the_boundary_is_visible_keeps_the_held_hooks(
    parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime(2026, 11, 6, 7, 54, 34, tzinfo=UTC)]
    monkeypatch.setattr(compaction_ownership, "_receive_time", lambda: clock[0])
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))
    ownership.route(_event(START, parent, source="compact"))

    # The settling event lands before Claude flushes the compact_boundary record.
    clock[0] += timedelta(seconds=3)
    assert ownership.route(_event(BEFORE, parent, prompt="early")) == ([], False)
    assert ownership.needs_transcripts(_event(BEFORE, parent, prompt="again")) is True

    _append(parent, _boundary("boundary-1"))
    clock[0] += timedelta(seconds=8)
    replays, held = ownership.route(_event(BEFORE, parent, prompt="late"))

    assert held is False
    assert [replay.event_type for replay in replays] == [PRE, START]


def test_held_hooks_are_dropped_once_the_grace_window_passes_without_a_boundary(
    parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime(2026, 11, 6, 7, 54, 34, tzinfo=UTC)]
    monkeypatch.setattr(compaction_ownership, "_receive_time", lambda: clock[0])
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))
    ownership.route(_event(START, parent, source="compact"))

    assert ownership.route(_event(BEFORE, parent, prompt="early")) == ([], False)

    clock[0] += timedelta(minutes=5)
    assert ownership.route(_event(BEFORE, parent, prompt="expired")) == ([], False)
    assert ownership.needs_transcripts(_event(BEFORE, parent, prompt="again")) is False

    _append(parent, _boundary("boundary-late"))
    clock[0] += timedelta(seconds=1)
    replays, _ = ownership.route(_event(BEFORE, parent, prompt="after"))

    assert replays == []


def test_a_long_compaction_gets_a_full_grace_window_from_its_session_start(
    parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime(2026, 11, 6, 7, 54, 34, tzinfo=UTC)]
    monkeypatch.setattr(compaction_ownership, "_receive_time", lambda: clock[0])
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))

    # Summarizing the conversation can take minutes between PreCompact and SessionStart.
    clock[0] += timedelta(minutes=3)
    ownership.route(_event(START, parent, source="compact"))

    clock[0] += timedelta(seconds=1)
    assert ownership.route(_event(BEFORE, parent, prompt="early")) == ([], False)
    assert ownership.needs_transcripts(_event(BEFORE, parent, prompt="again")) is True

    _append(parent, _boundary("boundary-1"))
    clock[0] += timedelta(seconds=3)
    replays, _ = ownership.route(_event(BEFORE, parent, prompt="late"))

    assert [replay.event_type for replay in replays] == [PRE, START]


def test_settle_replays_one_lifecycle_in_pre_start_post_order(parent: Path) -> None:
    ownership = CompactionOwnership()
    ownership.route(_event(POST, parent, trigger="auto"))
    ownership.route(_event(START, parent, source="compact"))
    ownership.route(_event(PRE, parent, trigger="auto"))
    _append(parent, _boundary("boundary-1"))
    _append(parent, _boundary("boundary-2"))
    _append(parent, _boundary("boundary-3"))

    replays, held = ownership.route(_event(BEFORE, parent, prompt="continue"))

    assert held is False
    assert [replay.event_type for replay in replays] == [PRE, START, POST]
    assert all(replay.metadata[REPLAY_FLAG] is True for replay in replays)


def test_replayed_events_are_not_held_again(parent: Path) -> None:
    ownership = CompactionOwnership()
    ownership.route(_event(START, parent, source="compact"))
    _append(parent, _boundary("boundary-1"))
    replays, _ = ownership.route(_event(BEFORE, parent, prompt="continue"))

    assert ownership.route(replays[0]) == ([], False)


def test_boundaries_before_the_buffer_began_are_baseline(parent: Path) -> None:
    _append(parent, _boundary("boundary-0"))
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))

    assert ownership.route(_event(BEFORE, parent, prompt="subagent-only")) == ([], False)

    ownership.route(_event(START, parent, source="compact"))
    _append(parent, _boundary("boundary-1"))
    replays, _ = ownership.route(_event(BEFORE, parent, prompt="parent"))

    # The auto PreCompact outlives the early settle: its boundary is the one that arrives later.
    assert [replay.event_type for replay in replays] == [PRE, START]


def test_a_known_baseline_ignores_timestamps(parent: Path) -> None:
    ownership = CompactionOwnership()
    ownership.route(_event(START, parent, source="compact"))
    _append(parent, _boundary("boundary-1", timestamp="2000-01-01T00:00:00.000Z"))

    replays, _ = ownership.route(_event(BEFORE, parent, prompt="continue"))

    assert [replay.event_type for replay in replays] == [START]


def test_subagent_transcript_boundaries_are_never_read(parent: Path) -> None:
    subagent_dir = parent.with_suffix("") / "subagents"
    subagent_dir.mkdir(parents=True)
    _append(
        subagent_dir / "agent-1.jsonl",
        _boundary("subagent-boundary-1", agentId="agent-1", isSidechain=True),
    )
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))

    assert ownership.route(_event(BEFORE, parent, prompt="continue")) == ([], False)


def test_malformed_and_lookalike_lines_are_not_boundaries(parent: Path) -> None:
    _append(parent, b'not json "compact_boundary"\n')
    _append(parent, b'["compact_boundary"]\n')
    _append(parent, _record(type="user", content="a compact_boundary mention"))
    _append(parent, _record(type="system", subtype="compact_boundary"))
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))

    assert ownership.route(_event(BEFORE, parent, prompt="junk only")) == ([], False)

    ownership.route(_event(PRE, parent, trigger="auto"))
    _append(parent, _boundary("boundary-1"))
    replays, _ = ownership.route(_event(BEFORE, parent, prompt="real boundary"))

    assert [replay.event_type for replay in replays] == [PRE]


def test_missing_transcript_keeps_the_held_hooks_for_a_later_read(tmp_path: Path) -> None:
    missing = tmp_path / "gone.jsonl"
    ownership = CompactionOwnership()

    assert ownership.route(_event(PRE, missing, trigger="auto")) == ([], True)
    assert ownership.route(_event(BEFORE, missing, prompt="continue")) == ([], False)
    assert ownership.needs_transcripts(_event(BEFORE, missing, prompt="again")) is True


def test_event_without_transcript_path_keeps_the_held_hooks(parent: Path) -> None:
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))

    assert ownership.route(_event(BEFORE, None, prompt="no path")) == ([], False)

    _append(parent, _boundary("boundary-1"))
    replays, _ = ownership.route(_event(BEFORE, parent, prompt="with path"))

    assert [replay.event_type for replay in replays] == [PRE]


@pytest.mark.parametrize("chunk_bytes", [1, 3, 17, 4096])
def test_backward_read_finds_every_boundary_at_any_chunk_size(
    parent: Path, monkeypatch: pytest.MonkeyPatch, chunk_bytes: int
) -> None:
    for uuid in ("boundary-1", "boundary-2", "boundary-3"):
        _append(parent, _boundary(uuid, timestamp="2026-11-03T18:41:03.088Z"))
    monkeypatch.setattr(compaction_ownership, "_READ_CHUNK_BYTES", chunk_bytes)

    found = compaction_ownership._boundaries_newest_first(parent, None, None)
    stopped = compaction_ownership._boundaries_newest_first(parent, "boundary-2", None)

    assert found == ["boundary-3", "boundary-2", "boundary-1"]
    assert stopped == ["boundary-3"]


def test_merge_prepends_replayed_context_to_the_settling_response() -> None:
    settling = HookResponse(decision="allow", context="own context")
    replayed = HookResponse(decision="allow", context="reset context")

    merged = merge_replayed_context(settling, [replayed])

    assert merged is settling
    assert merged.context is not None
    assert merged.context.index("reset context") < merged.context.index("own context")


def test_merge_without_replayed_context_leaves_the_response_unchanged() -> None:
    settling = HookResponse(decision="allow", context="own context")

    assert merge_replayed_context(settling, [HookResponse(decision="allow")]).context == (
        "own context"
    )


def test_a_manual_boundary_settles_once_at_the_next_main_event(parent: Path) -> None:
    ownership = CompactionOwnership()
    assert ownership.route(_event(PRE, parent, trigger="manual")) == ([], False)
    _append(parent, _boundary("manual-boundary"))
    assert ownership.route(_event(START, parent, source="compact")) == ([], True)

    replays, held = ownership.route(_event(BEFORE, parent, prompt="continue"))

    assert held is False
    assert [replay.event_type for replay in replays] == [START]
    assert ownership.route(_event(BEFORE, parent, prompt="again")) == ([], False)


def test_a_manual_pre_compact_leaves_the_held_lifecycle_for_the_next_main_event(
    parent: Path,
) -> None:
    ownership = CompactionOwnership()
    ownership.route(_event(PRE, parent, trigger="auto"))
    _append(parent, _boundary("auto-boundary"))
    ownership.route(_event(START, parent, source="compact"))

    assert ownership.route(_event(PRE, parent, trigger="manual")) == ([], False)

    replays, held = ownership.route(_event(BEFORE, parent, prompt="continue"))

    assert held is False
    assert [replay.event_type for replay in replays] == [START]


def test_held_pre_compact_is_not_replayed_when_a_manual_boundary_settles(parent: Path) -> None:
    # A manual PreCompact ran, a subagent's auto PreCompact and the manual tails were held, then
    # the manual boundary was written. The held PreCompact must not run the rules again.
    ownership = CompactionOwnership()
    assert ownership.route(_event(PRE, parent, trigger="manual")) == ([], False)
    ownership.route(_event(PRE, parent, trigger="auto"))
    ownership.route(_event(START, parent, source="compact"))
    ownership.route(_event(POST, parent, trigger="auto"))
    _append(parent, _boundary("manual-boundary"))

    replays, held = ownership.route(_event(BEFORE, parent, prompt="continue"))

    assert held is False
    assert [replay.event_type for replay in replays] == [START, POST]


def _unreadable(*_args: object) -> Any:
    raise OSError("transient read failure")


def test_each_route_decision_is_logged_with_its_hook_trigger_source_and_signal(
    parent: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger=compaction_ownership.logger.name)
    ownership = CompactionOwnership()

    ownership.route(_event(PRE, parent, trigger="auto"))
    ownership.route(_event(PRE, parent, trigger="manual"))
    ownership.route(_event(BEFORE, parent, prompt="batch"))
    _append(parent, _boundary("manual-boundary"))
    ownership.route(_event(START, parent, source="compact"))
    ownership.route(_event(BEFORE, parent, prompt="continue"))
    ownership.route(_event(START, None, source="compact"))
    ownership.route(_event(BEFORE, None, prompt="no transcript"))
    monkeypatch.setattr(compaction_ownership, "_lines_newest_first", _unreadable)
    ownership.route(_event(BEFORE, parent, prompt="unreadable"))

    routes = [message for message in caplog.messages if message.startswith("compaction route ")]
    assert routes == [
        (
            "compaction route held: hook=pre_compact trigger=auto source=- "
            "signal=awaiting_main_event session=session-under-test"
        ),
        (
            "compaction route manual: hook=pre_compact trigger=manual source=- "
            "signal=manual_pre_compact session=session-under-test"
        ),
        (
            "compaction route kept: hook=before_agent trigger=- source=- "
            "signal=awaiting_boundary held=pre_compact session=session-under-test"
        ),
        (
            "compaction route held: hook=session_start trigger=- source=compact "
            "signal=awaiting_main_event session=session-under-test"
        ),
        (
            "compaction route settled: hook=before_agent trigger=- source=- "
            "signal=parent_boundary boundaries=1 held=pre_compact,session_start replays=1 "
            "session=session-under-test"
        ),
        (
            "compaction route held: hook=session_start trigger=- source=compact "
            "signal=awaiting_main_event session=session-under-test"
        ),
        (
            "compaction route kept: hook=before_agent trigger=- source=- "
            "signal=no_transcript session=session-under-test"
        ),
        (
            "compaction route kept: hook=before_agent trigger=- source=- "
            "signal=unreadable_transcript session=session-under-test"
        ),
    ]
