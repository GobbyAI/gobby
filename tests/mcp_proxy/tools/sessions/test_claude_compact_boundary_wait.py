"""A Claude parent boundary completes the delivery wait while its lifecycle stays held."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.compaction_ownership import CompactionOwnership
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    _COMPACT_BOUNDARY_TIMEOUT_REASON,
    _wait_for_compact_boundary,
)
from gobby.sessions.compact_continuation import CompactBoundaryWaiter
from gobby.sessions.transcript_cursor import TranscriptTailCursor

pytestmark = pytest.mark.unit

SESSION_ID = "session-under-test"


class _Pane:
    async def snapshot(self, lines: int = 12, *, mode: str = "text") -> str | None:
        return None


def _event(event_type: HookEventType, transcript: Path, **data: Any) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"transcript_path": str(transcript), **data},
    )


def _boundary_line(uuid: str) -> str:
    return json.dumps({"type": "system", "subtype": "compact_boundary", "uuid": uuid}) + "\n"


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _waiter() -> CompactBoundaryWaiter:
    return CompactBoundaryWaiter(
        "attempt", "handoff", None, asyncio.Event(), asyncio.get_running_loop()
    )


def _receiving(waiter: CompactBoundaryWaiter, calls: list[str]) -> Callable[[], object]:
    def receive() -> bool:
        calls.append("boundary")
        waiter.loop.call_soon_threadsafe(waiter.event.set)
        return True

    return receive


@pytest.mark.asyncio
async def test_parent_boundary_completes_wait_while_postcompact_is_held(tmp_path: Path) -> None:
    transcript = tmp_path / "parent.jsonl"
    transcript.write_text(json.dumps({"type": "user", "uuid": "u0"}) + "\n", encoding="utf-8")
    cursor = TranscriptTailCursor.at_eof(transcript)
    ownership = CompactionOwnership()

    # A manual /compact on an idle pane: the daemon's PreCompact runs, the compaction
    # writes its parent boundary, then SessionStart(compact) and PostCompact arrive and
    # are held. No main-thread event follows, because the continuation is what would
    # produce one.
    ownership.route(_event(HookEventType.PRE_COMPACT, transcript, trigger="manual"))
    _append(transcript, _boundary_line("boundary-1"))
    ownership.route(_event(HookEventType.SESSION_START, transcript, source="compact"))
    ownership.route(_event(HookEventType.POST_COMPACT, transcript, trigger="manual"))

    waiter = _waiter()
    calls: list[str] = []
    result = await _wait_for_compact_boundary(
        waiter,
        _Pane(),
        None,
        cursor,
        timeout_seconds=1.0,
        on_claude_boundary=_receiving(waiter, calls),
    )

    assert result is None
    assert calls == ["boundary"]
    # The held lifecycle still settles on the continuation's own BEFORE_AGENT, so the
    # rules replay before that prompt is handled and its context merges there.
    replays, held = ownership.route(_event(HookEventType.BEFORE_AGENT, transcript))
    assert held is False
    assert [event.event_type for event in replays] == [
        HookEventType.SESSION_START,
        HookEventType.POST_COMPACT,
    ]


@pytest.mark.asyncio
async def test_subagent_boundary_does_not_complete_the_parent_wait(tmp_path: Path) -> None:
    parent = tmp_path / "parent.jsonl"
    parent.write_text(json.dumps({"type": "user", "uuid": "u0"}) + "\n", encoding="utf-8")
    subagent = tmp_path / "parent" / "subagents" / "agent-1.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_text("", encoding="utf-8")
    cursor = TranscriptTailCursor.at_eof(parent)
    _append(subagent, _boundary_line("subagent-boundary"))

    waiter = _waiter()
    calls: list[str] = []
    result = await _wait_for_compact_boundary(
        waiter,
        _Pane(),
        None,
        cursor,
        timeout_seconds=0.2,
        on_claude_boundary=_receiving(waiter, calls),
    )

    assert result == _COMPACT_BOUNDARY_TIMEOUT_REASON
    assert calls == []
