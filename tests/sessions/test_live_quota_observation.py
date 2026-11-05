"""Caught-up live transcript quota collection (plan 4.3).

Quota is observed only from raw typed Codex envelopes on a pass that starts at the
transcript end and reaches it again, and only inside the 900s freshness window. Each
envelope's stream cursor is its real byte offset, so its occurrence provenance names
the stream position. History, rebuilds, malformed or undated records, user/tool quotes
and replays never write a new reading or raise a new edge.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.app_context import ServiceContainer, clear_app_context, set_app_context
from gobby.providers.capacity_service import ProviderCapacityService, ProviderCapacitySnapshot
from gobby.providers.quota_alerts import AlertEdge, AlertSink
from gobby.providers.quota_observations import QuotaObservation
from gobby.providers.quota_signals import normalize_typed_signal
from gobby.runner import GobbyRunner
from gobby.runner_init.services import _build_message_processor
from gobby.sessions import processor_transcripts, turn_lifecycle
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcript_processing import TranscriptProcessingMixin
from gobby.storage.provider_capacity import PersistedCapacityState, ProviderCapacityRecord

pytestmark = pytest.mark.unit

SESSION_ID = "session-live-quota"
GENERATION = 3
_MACHINE_ID = "dddddddd-dddd-4ddd-8ddd-000000000001"
_REACHED: dict[str, object] = {
    "limit_id": "codex",
    "primary": None,
    "secondary": None,
    "rate_limit_reached_type": "rate_limit_reached",
    "plan_type": "pro",
}


def _ts(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _codex_line(timestamp: str, rate_limits: Mapping[str, object]) -> str:
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": rate_limits},
        }
    )


def _weekly(used: float) -> dict[str, object]:
    return {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 1790000000},
        "secondary": None,
        "plan_type": "pro",
    }


def _five_hour(used: float) -> dict[str, object]:
    return {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1789990000},
        "secondary": None,
        "plan_type": "pro",
    }


def _normalized(line: str, observed_at: str, *, cursor: int | None = None) -> QuotaObservation:
    observation = normalize_typed_signal(
        "codex",
        json.loads(line),
        observed_at=datetime.fromisoformat(observed_at),
        source_key=f"{SESSION_ID}:{GENERATION}",
        cursor=cursor,
    )
    assert observation is not None
    return observation


class _Attention:
    """Stands in for the attention store that holds the session's turn generation."""

    def get(self, entry_id: str) -> SimpleNamespace:
        return SimpleNamespace(payload={"turn_lifecycle": {"generation": GENERATION}})


class _Storage:
    """In-memory capacity storage with the same compare-and-set contract as the database."""

    machine_id = _MACHINE_ID

    def __init__(self) -> None:
        self.records: dict[str, ProviderCapacityRecord] = {}
        self.upsert_calls = 0

    def get(self, provider: str) -> ProviderCapacityRecord | None:
        return self.records.get(provider)

    def upsert(
        self,
        *,
        provider: str,
        state: PersistedCapacityState,
        observed_at: datetime,
        windows: Sequence[Mapping[str, object]],
        reason: str | None,
        source_version: str,
        details: Mapping[str, object],
        expected_observed_at: datetime | None,
    ) -> bool:
        self.upsert_calls += 1
        current = self.records.get(provider)
        if (None if current is None else current.observed_at) != expected_observed_at:
            return False
        self.records[provider] = ProviderCapacityRecord(
            machine_id=self.machine_id,
            provider=provider,
            state=state,
            observed_at=observed_at,
            windows=tuple(dict(window) for window in windows),
            reason=reason,
            source_version=source_version,
            age_seconds=0.0,
            details=dict(details),
        )
        return True


class _BlockingSink:
    """An operator channel that takes one edge and never finishes delivering it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, edge: AlertEdge) -> None:
        self.entered.set()
        await self.release.wait()


@pytest.fixture(autouse=True)
def _turn_generation_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the session's turn generation from a stored payload, as the reducer does."""
    monkeypatch.setattr(turn_lifecycle, "AttentionStateManager", lambda _db: _Attention())


class _Harness:
    """A processor over one Codex transcript, with a recording wrapper on the real service."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sink: AlertSink | None = None
    ) -> None:
        self.transcript = tmp_path / "codex.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.storage = _Storage()
        self.service = ProviderCapacityService(self.storage, alert_sink=sink or AsyncMock())
        self.observations: list[QuotaObservation] = []
        observe = self.service.observe

        async def recording_observe(observation: QuotaObservation) -> ProviderCapacitySnapshot:
            self.observations.append(observation)
            return await observe(observation)

        monkeypatch.setattr(self.service, "observe", recording_observe)
        self.processor = SessionMessageProcessor(MagicMock(), session_manager=MagicMock())
        self.processor.provider_capacity_service = self.service
        self.processor.register_session(SESSION_ID, str(self.transcript), source="codex")

    def append(self, *lines: str) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    async def process(self) -> bool:
        return await self.processor._process_session(SESSION_ID, str(self.transcript))

    def observed(self) -> list[QuotaObservation]:
        return list(self.observations)


@pytest.mark.asyncio
async def test_quota_only_live_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    # The first pass reaches the transcript end, so later appended bytes are live.
    assert await harness.process()

    # Each envelope timestamp is captured once so the expected instants match the lines.
    weekly_older, weekly_newer, five_hour_ts = _ts(120), _ts(60), _ts(300)
    newer_line = _codex_line(weekly_newer, _weekly(80.0))
    harness.append(
        _codex_line(weekly_older, _weekly(70.0)),
        newer_line,
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
    assert weekly.scope == f"source:{SESSION_ID}:{GENERATION}"
    assert by_label["five_hour"].source_observed_at == datetime.fromisoformat(five_hour_ts)

    # The occurrence is the typed reading at the envelope's real byte offset in the transcript.
    newer_offset = harness.transcript.read_bytes().index((newer_line + "\n").encode())
    (expected,) = _normalized(newer_line, weekly_newer, cursor=newer_offset).components
    assert weekly.occurrence == expected.occurrence
    (unpositioned,) = _normalized(newer_line, weekly_newer).components
    assert weekly.occurrence != unpositioned.occurrence

    # A partial batch carries only the observed component and never refreshes siblings.
    harness.append(_codex_line(_ts(30), _five_hour(15.0)))
    assert await harness.process()
    (partial,) = harness.observed()[1:]
    assert {c.window.label for c in partial.components if c.window is not None} == {"five_hour"}

    # A windowless reached reading is its own exhausted component and carries no window.
    harness.append(_codex_line(_ts(10), _REACHED))
    assert await harness.process()
    (reached,) = harness.observed()[2:]
    (hard,) = reached.components
    assert hard.window is None and hard.state == "exhausted"


@pytest.mark.asyncio
async def test_history_is_not_live_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(processor_transcripts, "CATCHUP_CHUNK_BYTES", 256)
    harness = _Harness(tmp_path, monkeypatch)

    # A backlog of fresh quota lines is history until the processor reaches the end.
    harness.append(*(_codex_line(_ts(60 + i), _weekly(50.0 + i)) for i in range(4)))
    caught_up = False
    for _ in range(20):
        caught_up = await harness.process()
        if caught_up:
            break
    assert caught_up
    assert harness.observed() == []
    assert harness.storage.upsert_calls == 0

    # The end is reached, so a live append is collected and writes one reading.
    live_line = _codex_line(_ts(10), _weekly(90.0))
    harness.append(live_line)
    assert await harness.process()
    assert len(harness.observed()) == 1
    assert harness.storage.upsert_calls == 1

    # The same envelope bytes appended again sit at a new cursor. The service treats the
    # replay as a duplicate reading, so nothing is written and no edge is raised.
    harness.append(live_line)
    assert await harness.process()
    assert harness.storage.upsert_calls == 1

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
    observed_before = len(harness.observed())
    harness.append(
        "{not json",
        undated,
        user_quote,
        _codex_line(_ts(2 * 900), _weekly(100.0)),
    )
    assert await harness.process()
    assert len(harness.observed()) == observed_before
    assert harness.storage.upsert_calls == 1

    # Replaying the same cursor sends nothing.
    assert await harness.process()
    assert len(harness.observed()) == observed_before
    assert harness.storage.upsert_calls == 1


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
    storage = _Storage()
    sink = _BlockingSink()
    shared = ProviderCapacityService(storage, alert_sink=sink)
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
            handle.write(_codex_line(_ts(10), _REACHED) + "\n")
        # The reached edge goes to an operator channel that never finishes. The pass still
        # completes and admits the reading, so admission does not wait on delivery.
        assert await asyncio.wait_for(
            processor._process_session(SESSION_ID, str(transcript)), timeout=5
        )
        await asyncio.wait_for(sink.entered.wait(), timeout=5)
        assert storage.upsert_calls == 1
        assert "codex" in storage.records
    finally:
        sink.release.set()
        clear_app_context()


class _RebuildHost(TranscriptProcessingMixin):
    """Just enough of the lifecycle host for the rebuild writer, with a capacity service."""

    def __init__(self, transcript: Path, service: ProviderCapacityService) -> None:
        self.db = MagicMock()
        self.session_manager = MagicMock()
        session = MagicMock(
            source="codex", transcript_path=str(transcript), project_id=None, context_window=None
        )
        session.model = None
        self.session_manager.get.return_value = session
        self.token_event_store = MagicMock()
        self.provider_capacity_service = service


def test_rebuild_writer_publishes_no_live_quota(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    user_turn = json.dumps(
        {
            "timestamp": _ts(20),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        }
    )
    transcript.write_text(
        "\n".join([user_turn, _codex_line(_ts(10), _REACHED)]) + "\n", encoding="utf-8"
    )
    storage = _Storage()
    host = _RebuildHost(transcript, ProviderCapacityService(storage))

    # The rebuild reads the whole transcript, including the fresh reached envelope.
    host._persist_session_transcript(SESSION_ID, str(transcript))
    cast(MagicMock, host.session_manager.get).assert_called_once_with(SESSION_ID)
    # Nothing is admitted, so the capacity store is never written.
    assert storage.upsert_calls == 0
    assert storage.records == {}
