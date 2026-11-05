"""Caught-up live transcript quota collection (plan 4.3).

Quota is observed only from raw typed Codex envelopes on a pass that starts at the
transcript end and reaches it again, and only inside the 900s freshness window.
History, rebuilds, malformed or undated lines, user/tool content and replays never
produce a new observation.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.app_context import ServiceContainer, clear_app_context, set_app_context
from gobby.providers.capacity_service import ProviderCapacityService
from gobby.runner import GobbyRunner
from gobby.runner_init.services import _build_message_processor
from gobby.sessions import processor_transcripts
from gobby.sessions.processor import SessionMessageProcessor

pytestmark = pytest.mark.unit

SESSION_ID = "session-live-quota"


def _ts(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _codex_line(timestamp: str, rate_limits: dict[str, Any]) -> str:
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": rate_limits},
        }
    )


def _weekly(used: float) -> dict[str, Any]:
    return {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 1790000000},
        "secondary": None,
        "plan_type": "pro",
    }


def _five_hour(used: float) -> dict[str, Any]:
    return {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1789990000},
        "secondary": None,
        "plan_type": "pro",
    }


class _Harness:
    """A processor over one Codex transcript with a recording capacity service."""

    def __init__(self, tmp_path: Path) -> None:
        self.transcript = tmp_path / "codex.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.session_manager = MagicMock()
        self.capacity = MagicMock()
        self.capacity.observe = AsyncMock()
        self.processor = SessionMessageProcessor(MagicMock(), session_manager=self.session_manager)
        self.processor.provider_capacity_service = self.capacity
        self.processor.register_session(SESSION_ID, str(self.transcript), source="codex")

    def append(self, *lines: str) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    async def process(self) -> bool:
        return await self.processor._process_session(SESSION_ID, str(self.transcript))

    def observed(self) -> list[Any]:
        return [call.args[0] for call in self.capacity.observe.await_args_list]


@pytest.mark.asyncio
async def test_quota_only_live_records(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    # The first pass reaches the transcript end, so later appended bytes are live.
    assert await harness.process()

    # Each envelope timestamp is captured once so the expected instants match the lines.
    weekly_older, weekly_newer, five_hour_ts = _ts(120), _ts(60), _ts(300)
    harness.append(
        _codex_line(weekly_older, _weekly(70.0)),
        _codex_line(weekly_newer, _weekly(80.0)),
        _codex_line(five_hour_ts, _five_hour(12.0)),
    )
    assert await harness.process()

    # Quota-only envelopes reach observe with no token usage in the transcript.
    (observation,) = harness.observed()
    by_label = {c.window.label: c for c in observation.components if c.window is not None}
    assert set(by_label) == {"weekly", "five_hour"}
    weekly = by_label["weekly"]
    # Newest wins per window: the 60s-old 80% reading replaces the 120s-old 70% one.
    assert weekly.window is not None and weekly.window.used == 80.0
    assert weekly.source_observed_at == datetime.fromisoformat(weekly_newer)
    assert weekly.scope == f"source:{SESSION_ID}:0"
    assert by_label["five_hour"].source_observed_at == datetime.fromisoformat(five_hour_ts)

    # A partial batch carries only the observed component and never refreshes siblings.
    harness.append(_codex_line(_ts(30), _five_hour(15.0)))
    assert await harness.process()
    (partial,) = harness.observed()[1:]
    assert {c.window.label for c in partial.components if c.window is not None} == {"five_hour"}


@pytest.mark.asyncio
async def test_history_is_not_live_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(processor_transcripts, "CATCHUP_CHUNK_BYTES", 256)
    harness = _Harness(tmp_path)

    # A backlog of fresh quota lines is history until the processor reaches the end.
    harness.append(*(_codex_line(_ts(60 + i), _weekly(50.0 + i)) for i in range(4)))
    caught_up = False
    for _ in range(20):
        caught_up = await harness.process()
        if caught_up:
            break
    assert caught_up
    assert harness.capacity.observe.await_count == 0

    # The end is reached, so live appends are collected again.
    harness.append(_codex_line(_ts(10), _weekly(90.0)))
    assert await harness.process()
    assert harness.capacity.observe.await_count == 1

    # Drop the small chunk so the next pass reads every bad line and reaches the end; the
    # caught-up latch from the live pass then stays in force for these lines.
    monkeypatch.undo()

    # Malformed, undated, user-quoted and stale lines never produce an observation.
    user_quote = json.dumps(
        {
            "timestamp": _ts(5),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "rate_limits weekly 100.0"}],
            },
        }
    )
    undated = json.dumps(
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": _weekly(99.0)}}
    )
    harness.append(
        "{not json",
        undated,
        user_quote,
        _codex_line(_ts(2 * 900), _weekly(100.0)),
    )
    assert await harness.process()
    assert harness.capacity.observe.await_count == 1

    # Replaying the same cursor sends nothing.
    assert await harness.process()
    assert harness.capacity.observe.await_count == 1


@pytest.mark.asyncio
async def test_rebuilt_processor_keeps_capacity_service(tmp_path: Path) -> None:
    config = MagicMock()
    config.message_tracking.enabled = True
    config.message_tracking.poll_interval = 5.0
    runner = SimpleNamespace(
        database=MagicMock(),
        session_manager=MagicMock(),
        db_executor=SimpleNamespace(run=asyncio.to_thread),
        websocket_server=MagicMock(),
        http_server=SimpleNamespace(_hook_manager=MagicMock()),
        message_processor=None,
        communications_manager=None,
    )
    shared = MagicMock(spec=ProviderCapacityService)
    set_app_context(cast(ServiceContainer, SimpleNamespace(provider_capacity_service=shared)))
    try:
        loop = asyncio.get_running_loop()
        prepared = _build_message_processor(cast(GobbyRunner, runner), config, loop)
        assert prepared is not None
        processor = prepared.value
        assert isinstance(processor, SessionMessageProcessor)
        with (
            patch.object(SessionMessageProcessor, "start", AsyncMock()),
            patch.object(SessionMessageProcessor, "stop", AsyncMock()),
        ):
            await asyncio.to_thread(prepared.activate)

        # The rebuilt processor observes into the exact shared service.
        assert processor.provider_capacity_service is shared
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text("", encoding="utf-8")
        processor.register_session(SESSION_ID, str(transcript), source="codex")
        assert await processor._process_session(SESSION_ID, str(transcript))
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_codex_line(_ts(30), _weekly(64.0)) + "\n")
        assert await processor._process_session(SESSION_ID, str(transcript))
        shared.observe.assert_awaited_once()
    finally:
        clear_app_context()
