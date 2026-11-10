"""Provider-reported run totals reach the session ledger (plan 2.3, #23600)."""

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.cli import tokens as tokens_cli
from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.reported_usage import (
    ReportedRun,
    claude_cost_runs,
    droid_sidecar_run,
    grok_turn_runs,
)
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.reported_usage import ReportedUsageStore
from gobby.storage.sessions import SessionManager
from tests.config_runtime_helpers import static_session_capture
from tests.fixtures.postgres import TEST_USER_ID

PROJECT_ID = "00000000-0000-4000-8000-000000000230"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000230"
TS = "2026-10-20T10:00:00Z"
START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
START_MS = int(START.timestamp() * 1000)


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Any:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "Reported Usage"),
    )
    LocalMachineManager(hub_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID)
    return hub_db


@pytest.fixture
def session_manager(db: HubDatabase) -> SessionManager:
    return SessionManager(db)


@pytest.fixture
def lifecycle_manager(db: HubDatabase) -> SessionLifecycleManager:
    return SessionLifecycleManager(db, static_session_capture(SessionLifecycleConfig()))


def _user_line() -> str:
    record = {"type": "user", "timestamp": TS, "message": {"role": "user", "content": "hi"}}
    return json.dumps(record)


def _cost_state(start_ms: int, duration_ms: int, cost: float) -> str:
    record = {
        "type": "cost-state",
        "startTime": start_ms,
        "totalCostUSD": cost,
        "totalAPIDuration": duration_ms // 2,
        "totalDuration": duration_ms,
        "hasUnknownModelCost": False,
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 2}},
    }
    return json.dumps(record)


def _grok_turn(prompt_id: str, ticks: float, *, incomplete: bool = False) -> str:
    usage = {
        "inputTokens": 100,
        "outputTokens": 20,
        "cachedReadTokens": 0,
        "cacheCreationTokens": 0,
        "costUsdTicks": ticks,
        "apiDurationMs": 1500,
        "modelUsage": {"grok-build": {"costUsdTicks": ticks}},
        "usageIsIncomplete": incomplete,
    }
    update = {"sessionUpdate": "turn_completed", "prompt_id": prompt_id, "usage": usage}
    return json.dumps({"timestamp": TS, "params": {"update": update}})


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))


def _write_sidecar(path: Path, credits: float, active_ms: int, mtime: datetime) -> None:
    data = {"tokenUsage": {"factoryCredits": credits}, "assistantActiveTimeMs": active_ms}
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def _register(
    session_manager: SessionManager,
    external_id: str,
    transcript: Path,
    source: str = "claude",
) -> Any:
    return session_manager.register(
        external_id=external_id,
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id=PROJECT_ID,
        title=external_id,
        transcript_path=str(transcript),
    )


def _processor(db: HubDatabase, session_manager: SessionManager) -> SessionMessageProcessor:
    return SessionMessageProcessor(db, session_manager=session_manager)


async def _pass(processor: SessionMessageProcessor, session: Any, transcript: Path) -> None:
    assert await processor._process_session(session.id, str(transcript))


def _xmin(db: HubDatabase, session_id: str, run_key: str) -> str:
    row = db.fetchone(
        "SELECT xmin::text AS xmin FROM session_reported_usage WHERE session_id = %s AND run_key = %s",
        (session_id, run_key),
    )
    assert row is not None
    return str(row["xmin"])


_BAD_NUMBERS = [float("nan"), float("inf"), -1.0]
_BAD_IDS = ["nan", "inf", "negative"]


@pytest.mark.parametrize("bad", _BAD_NUMBERS, ids=_BAD_IDS)
def test_claude_rejects_non_finite_or_negative_cost(bad: float) -> None:
    good = _cost_state(START_MS, 60_000, 0.25)
    rejected = _cost_state(START_MS + 3_600_000, 60_000, bad)

    runs = claude_cost_runs([good, rejected])
    assert [run.run_key for run in runs] == [f"claude:{START_MS}"]
    assert runs[0].cost_amount == Decimal("0.25")


def test_claude_rejects_out_of_range_start_time() -> None:
    good = _cost_state(START_MS, 60_000, 0.25)
    out_of_range = json.dumps(
        {"type": "cost-state", "startTime": 1e300, "totalCostUSD": 0.25, "totalDuration": 60_000}
    )
    runs = claude_cost_runs([good, out_of_range])
    assert [run.run_key for run in runs] == [f"claude:{START_MS}"]


def test_claude_bounds_api_duration_to_bigint() -> None:
    record = json.dumps(
        {
            "type": "cost-state",
            "startTime": START_MS,
            "totalCostUSD": 0.25,
            "totalDuration": 60_000,
            "totalAPIDuration": 1e19,
        }
    )
    [run] = claude_cost_runs([record])
    assert run.api_duration_ms is None
    assert run.wall_duration_ms == 60_000


def test_claude_rejects_non_finite_start_time() -> None:
    record = json.dumps({"type": "cost-state", "startTime": float("nan"), "totalCostUSD": 0.25})
    assert claude_cost_runs([record]) == []


def test_claude_ignores_non_finite_duration() -> None:
    record = json.dumps(
        {
            "type": "cost-state",
            "startTime": START_MS,
            "totalCostUSD": 0.25,
            "totalDuration": float("nan"),
        }
    )
    [run] = claude_cost_runs([record])
    assert run.cost_amount == Decimal("0.25")
    assert run.wall_duration_ms is None
    assert run.observed_at == run.started_at


@pytest.mark.parametrize(
    ("start_ms", "duration_ms"),
    [(START_MS, 1e17), (253_402_300_799_000, 60_000)],
    ids=["duration-beyond-timedelta", "end-beyond-datetime"],
)
def test_claude_keeps_cost_when_timeline_overflows(start_ms: int, duration_ms: float) -> None:
    record = json.dumps(
        {
            "type": "cost-state",
            "startTime": start_ms,
            "totalCostUSD": 0.25,
            "totalDuration": duration_ms,
        }
    )
    [run] = claude_cost_runs([record])
    assert run.cost_amount == Decimal("0.25")
    assert run.wall_duration_ms is None
    assert run.observed_at == run.started_at


@pytest.mark.parametrize("bad", _BAD_NUMBERS, ids=_BAD_IDS)
def test_grok_treats_non_finite_or_negative_ticks_as_no_cost(bad: float) -> None:
    parser = GrokTranscriptParser(session_id="grok-reported")
    message = parser.parse_line(_grok_turn("prompt-bad", bad), 0)
    assert isinstance(message, ParsedMessage)

    [run] = grok_turn_runs([message])
    assert run.cost_unit is None
    assert run.cost_amount is None


@pytest.mark.parametrize("bad", _BAD_NUMBERS, ids=_BAD_IDS)
def test_droid_rejects_non_finite_or_negative_credits(tmp_path: Path, bad: float) -> None:
    transcript = tmp_path / "droid-session.jsonl"
    transcript.write_text("", encoding="utf-8")
    _write_sidecar(
        tmp_path / "droid-session.settings.json",
        credits=bad,
        active_ms=4500,
        mtime=START + timedelta(minutes=30),
    )
    assert droid_sidecar_run(transcript, START) is None


def test_claude_cost_state_runs_are_per_start_time() -> None:
    first_start = START_MS
    resumed_start = START_MS + 3_600_000
    lines = [
        _cost_state(first_start, 60_000, 1.5),
        _cost_state(first_start, 120_000, 2.25),
        _cost_state(resumed_start, 30_000, 0.5),
    ]

    for ordered in (lines, list(reversed(lines))):
        runs = {run.run_key: run for run in claude_cost_runs(ordered)}
        assert set(runs) == {f"claude:{first_start}", f"claude:{resumed_start}"}

        first = runs[f"claude:{first_start}"]
        assert first.cost_unit == "usd"
        assert first.cost_amount == Decimal("2.25")
        assert first.started_at == START
        assert first.observed_at == START + timedelta(milliseconds=120_000)
        assert first.wall_duration_ms == 120_000

        resumed = runs[f"claude:{resumed_start}"]
        assert resumed.cost_amount == Decimal("0.5")
        assert resumed.started_at == START + timedelta(hours=1)


def test_grok_turn_runs_convert_ticks() -> None:
    parser = GrokTranscriptParser(session_id="grok-reported")
    complete = parser.parse_line(_grok_turn("prompt-1", 1_234_567_890), 0)
    incomplete = parser.parse_line(_grok_turn("prompt-2", 5_000_000_000, incomplete=True), 1)
    assert isinstance(complete, ParsedMessage)
    assert isinstance(incomplete, ParsedMessage)

    runs = {run.run_key: run for run in grok_turn_runs([complete, incomplete])}
    assert set(runs) == {f"grok:{complete.message_id}", f"grok:{incomplete.message_id}"}

    exact = runs[f"grok:{complete.message_id}"]
    assert exact.cost_unit == "usd"
    assert exact.cost_amount == Decimal("0.123456789")
    assert exact.cost_complete is True
    assert exact.api_duration_ms == 1500
    assert exact.started_at == exact.observed_at == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

    flagged = runs[f"grok:{incomplete.message_id}"]
    assert flagged.cost_amount == Decimal("0.5")
    assert flagged.cost_complete is False


def test_droid_sidecar_run(tmp_path: Path) -> None:
    transcript = tmp_path / "droid-session.jsonl"
    transcript.write_text("", encoding="utf-8")
    sidecar = tmp_path / "droid-session.settings.json"
    mtime = START + timedelta(minutes=30)
    _write_sidecar(sidecar, credits=12.5, active_ms=4500, mtime=mtime)

    run = droid_sidecar_run(transcript, START)
    assert run is not None
    assert run.run_key == "droid:session"
    assert run.cost_unit == "factory_credits"
    assert run.cost_amount == Decimal("12.5")
    assert run.wall_duration_ms == 4500
    assert run.started_at == START
    assert run.observed_at == mtime

    assert droid_sidecar_run(tmp_path / "absent.jsonl", START) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_keeps_newest_and_paths_agree(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    store = ReportedUsageStore(db)

    def claude_run(observed_seconds: int, cost: str) -> ReportedRun:
        return ReportedRun(
            run_key=f"claude:{START_MS}",
            source="claude",
            cost_unit="usd",
            cost_amount=Decimal(cost),
            cost_complete=True,
            api_duration_ms=None,
            wall_duration_ms=observed_seconds * 1000,
            started_at=START,
            observed_at=START + timedelta(seconds=observed_seconds),
        )

    direct_path = tmp_path / "direct.jsonl"
    _write(direct_path, [_user_line()])
    direct = _register(session_manager, "upsert-direct", direct_path)
    newer = claude_run(120, "2.25")
    store.upsert_runs(direct.id, [newer])
    store.upsert_runs(direct.id, [claude_run(60, "1.5")])
    assert store.list_runs(direct.id) == [newer]

    # Two cost-state records tie on totalCostUSD; the later reading (longer duration) wins.
    cost_lines = [
        _cost_state(START_MS, 60_000, 1.5),
        _cost_state(START_MS, 120_000, 1.5),
    ]
    live_path = tmp_path / "live.jsonl"
    _write(live_path, [_user_line(), *cost_lines])
    live = _register(session_manager, "upsert-live", live_path)
    processor = _processor(db, session_manager)
    processor.register_session(live.id, str(live_path), source="claude")
    await _pass(processor, live, live_path)

    rebuild_path = tmp_path / "rebuild.jsonl"
    _write(rebuild_path, [_user_line(), *cost_lines])
    rebuild = _register(session_manager, "upsert-rebuild", rebuild_path)
    await lifecycle_manager._process_session_transcript(rebuild.id, str(rebuild_path))

    live_runs = store.list_runs(live.id)
    assert [run.observed_at for run in live_runs] == [START + timedelta(seconds=120)]
    assert live_runs[0].wall_duration_ms == 120_000
    assert store.list_runs(rebuild.id) == live_runs

    # An audit of a session with no stored run reports it missing, and writes nothing.
    audit_path = tmp_path / "audit.jsonl"
    _write(audit_path, [_user_line(), *cost_lines])
    audited = _register(session_manager, "upsert-audit", audit_path)
    before = tokens_cli._audit_session(db, audited.id, fix=False)
    assert (before.missing_runs, before.stale_runs) == (1, 0)
    assert store.list_runs(audited.id) == []

    # --fix upserts the run, after which the audit is clean and the rows match the live path.
    repaired = tokens_cli._audit_session(db, audited.id, fix=True)
    assert repaired.repaired
    assert store.list_runs(audited.id) == live_runs
    after = tokens_cli._audit_session(db, audited.id, fix=False)
    assert (after.stale_runs, after.missing_runs) == (0, 0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idle_pass_refreshes_droid_sidecar(
    db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
) -> None:
    store = ReportedUsageStore(db)
    transcript = tmp_path / "droid-idle.jsonl"
    _write(transcript, [_user_line()])
    session = _register(session_manager, "droid-idle", transcript, source="droid")
    created = session.created_at
    sidecar = transcript.with_suffix(".settings.json")
    _write_sidecar(sidecar, credits=1.0, active_ms=1000, mtime=created + timedelta(seconds=1))

    processor = _processor(db, session_manager)
    processor.register_session(session.id, str(transcript), source="droid")
    await _pass(processor, session, transcript)
    [first] = store.list_runs(session.id)
    assert first.cost_amount == Decimal("1")

    # The transcript stays unchanged; only the sidecar grows and gets a later mtime.
    _write_sidecar(sidecar, credits=2.5, active_ms=2000, mtime=created + timedelta(seconds=2))
    await _pass(processor, session, transcript)
    [refreshed] = store.list_runs(session.id)
    assert refreshed.cost_amount == Decimal("2.5")
    assert refreshed.observed_at == created + timedelta(seconds=2)

    # With the sidecar unchanged, a further idle pass leaves the row untouched.
    xmin = _xmin(db, session.id, "droid:session")
    await _pass(processor, session, transcript)
    assert _xmin(db, session.id, "droid:session") == xmin
    assert store.list_runs(session.id) == [refreshed]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cost_state_only_append_reaches_the_ledger(
    db: HubDatabase,
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ReportedUsageStore(db)
    parent = tmp_path / "cost-only.jsonl"
    _write(parent, [_user_line()])
    session = _register(session_manager, "cost-only", parent)
    processor = _processor(db, session_manager)
    processor.register_session(session.id, str(parent), source="claude")
    await _pass(processor, session, parent)
    settled = parent.stat().st_size
    assert store.list_runs(session.id) == []

    # A transcript whose only new line is cost-state leaves through the no-message exit.
    _append(parent, [_cost_state(START_MS, 60_000, 1.5)])
    await _pass(processor, session, parent)
    [run] = store.list_runs(session.id)
    assert run.run_key == f"claude:{START_MS}"
    assert run.cost_amount == Decimal("1.5")
    assert processor._byte_offsets[session.id] == parent.stat().st_size

    # Replaying the pass from the earlier offset rewrites nothing.
    xmin = _xmin(db, session.id, run.run_key)
    processor._byte_offsets[session.id] = settled
    await _pass(processor, session, parent)
    assert store.list_runs(session.id) == [run]
    assert _xmin(db, session.id, run.run_key) == xmin

    # A ledger fault on that exit leaves the offset and parser state where they were.
    _append(parent, [_cost_state(START_MS, 120_000, 2.0)])
    offset_before = processor._byte_offsets[session.id]
    state_before = processor._parsers[session.id].snapshot_state()
    original = ReportedUsageStore.upsert_runs
    faults = [RuntimeError("injected ledger fault")]

    def upsert_failing_once(self: ReportedUsageStore, session_id: str, runs: Any) -> None:
        if faults:
            raise faults.pop()
        original(self, session_id, runs)

    monkeypatch.setattr(ReportedUsageStore, "upsert_runs", upsert_failing_once)
    with pytest.raises(RuntimeError, match="injected ledger fault"):
        await processor._process_session(session.id, str(parent))
    assert processor._byte_offsets[session.id] == offset_before
    assert processor._parsers[session.id].snapshot_state() == state_before
    assert store.list_runs(session.id) == [run]

    # The next pass ingests the line that failed.
    await _pass(processor, session, parent)
    [ingested] = store.list_runs(session.id)
    assert ingested.cost_amount == Decimal("2.0")
    assert processor._byte_offsets[session.id] == parent.stat().st_size


@pytest.mark.integration
@pytest.mark.parametrize("bad", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_grok_non_finite_fixture_persists(
    db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
    bad: float,
) -> None:
    # The fixture puts the same ticks at the top level and in modelUsage; jsonb rejects both.
    parser = GrokTranscriptParser(session_id="grok-persist")
    message = parser.parse_line(_grok_turn("prompt-bad", bad), 0)
    assert isinstance(message, ParsedMessage)
    transcript = tmp_path / "grok-persist.jsonl"
    _write(transcript, [_grok_turn("prompt-bad", bad)])
    session = _register(session_manager, "grok-persist", transcript, source="grok")

    store = ReportedUsageStore(db)
    store.upsert_runs(session.id, grok_turn_runs([message]))
    [stored] = store.list_runs(session.id)
    assert stored.cost_amount is None
    assert stored.model_usage == {"grok-build": {}}


@pytest.mark.integration
def test_non_finite_model_usage_keeps_valid_cost(
    db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
) -> None:
    # A finite cost survives; the NaN inside modelUsage is dropped and its finite siblings kept.
    record: dict[str, Any] = {
        "type": "cost-state",
        "startTime": START_MS,
        "totalCostUSD": 1.5,
        "totalAPIDuration": 30_000,
        "totalDuration": 60_000,
        "hasUnknownModelCost": False,
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": float("nan")}},
    }
    line = json.dumps(record)
    transcript = tmp_path / "claude-persist.jsonl"
    _write(transcript, [line])
    session = _register(session_manager, "claude-persist", transcript)

    store = ReportedUsageStore(db)
    store.upsert_runs(session.id, claude_cost_runs([line]))
    [stored] = store.list_runs(session.id)
    assert stored.cost_amount == Decimal("1.5")
    assert stored.model_usage == {"claude-haiku-5-5": {"inputTokens": 10}}
