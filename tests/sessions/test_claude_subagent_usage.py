"""Claude subagent calls join the parent session's usage ledger (plan 2.2, #23599)."""

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from gobby.cli import tokens as tokens_cli
from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions import subagent_usage
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.subagent_usage import SubagentCursor, read_subagent_events
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import TokenEventStore
from tests.config_runtime_helpers import static_session_capture
from tests.fixtures.postgres import TEST_USER_ID

PROJECT_ID = "00000000-0000-4000-8000-000000000223"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-00000000023b"
TS = "2026-10-20T10:00:00Z"

Batch = tuple[list[str | None], list[bool]]


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Any:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "Subagent Usage"),
    )
    LocalMachineManager(hub_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID)
    return hub_db


@pytest.fixture
def session_manager(db: HubDatabase) -> SessionManager:
    return SessionManager(db)


@pytest.fixture
def lifecycle_manager(db: HubDatabase) -> SessionLifecycleManager:
    return SessionLifecycleManager(db, static_session_capture(SessionLifecycleConfig()))


@pytest.fixture
def batches(monkeypatch: pytest.MonkeyPatch) -> list[Batch]:
    """Record each ledger write as its message ids and the per-row insert flags."""
    recorded: list[Batch] = []
    original = TokenEventStore.record_batch

    def spy(self: TokenEventStore, events: Any, **kwargs: Any) -> list[bool]:
        flags = original(self, events, **kwargs)
        recorded.append(([event.message_id for event in events], flags))
        return flags

    monkeypatch.setattr(TokenEventStore, "record_batch", spy)
    return recorded


def _user_line() -> str:
    record = {"type": "user", "timestamp": TS, "message": {"role": "user", "content": "hi"}}
    return json.dumps(record)


def _usage_line(message_id: str | None, input_tokens: int, output_tokens: int) -> str:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    if message_id is not None:
        message["id"] = message_id
    return json.dumps({"type": "assistant", "timestamp": TS, "message": message})


def _subagent_file(transcript: Path, agent_id: str) -> Path:
    directory = transcript.with_suffix("") / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"agent-{agent_id}.jsonl"


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(f"{line}\n" for line in lines)


def _register(session_manager: SessionManager, external_id: str, transcript: Path) -> Any:
    return session_manager.register(
        external_id=external_id,
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=PROJECT_ID,
        title=external_id,
        transcript_path=str(transcript),
    )


def _processor(db: HubDatabase, session_manager: SessionManager) -> SessionMessageProcessor:
    return SessionMessageProcessor(db, session_manager=session_manager)


async def _pass(processor: SessionMessageProcessor, session: Any, transcript: Path) -> None:
    processor.register_session(session.id, str(transcript), source="claude")
    assert await processor._process_session(session.id, str(transcript))


def _rows(db: HubDatabase, session_id: str) -> list[dict[str, Any]]:
    return TokenEventStore(db).list_session_events(session_id)


def _ids(db: HubDatabase, session_id: str) -> list[str]:
    return sorted(str(row["message_id"]) for row in _rows(db, session_id))


def _totals(session_manager: SessionManager, session_id: str) -> tuple[int, int]:
    session = session_manager.get(session_id)
    assert session is not None
    return session.usage_input_tokens, session.usage_output_tokens


def _occupancy(session_manager: SessionManager, session_id: str) -> tuple[Any, ...]:
    session = session_manager.get(session_id)
    assert session is not None
    return (
        session.context_used_tokens,
        session.context_usage_confidence,
        session.context_window,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subagent_calls_join_parent_ledger(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    subagent_lines = [_usage_line("sub-1", 100, 10), _usage_line("sub-2", 200, 20)]
    expected = {"sub-1": (100, 10), "sub-2": (200, 20)}

    def assert_joined(session_id: str) -> None:
        rows = _rows(db, session_id)
        assert {row["message_id"]: row["metadata"]["agent_id"] for row in rows} == {
            "sub-1": "a1",
            "sub-2": "a1",
        }
        assert {
            row["message_id"]: (row["input_tokens"], row["output_tokens"]) for row in rows
        } == expected
        assert _totals(session_manager, session_id) == (300, 30)

    live_parent = tmp_path / "live.jsonl"
    _write(live_parent, [_user_line()])
    _write(_subagent_file(live_parent, "a1"), subagent_lines)
    live = _register(session_manager, "live-subagent", live_parent)
    await _pass(_processor(db, session_manager), live, live_parent)
    assert_joined(live.id)

    rebuild_parent = tmp_path / "rebuild.jsonl"
    _write(rebuild_parent, [_user_line()])
    _write(_subagent_file(rebuild_parent, "a1"), subagent_lines)
    rebuild = _register(session_manager, "rebuild-subagent", rebuild_parent)
    await lifecycle_manager._process_session_transcript(rebuild.id, str(rebuild_parent))
    assert_joined(rebuild.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_offsets_read_each_line_once(
    db: HubDatabase,
    session_manager: SessionManager,
    batches: list[Batch],
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [_user_line()])
    subagent = _subagent_file(parent, "a1")
    _write(subagent, [_usage_line("sub-1", 10, 1)])
    session = _register(session_manager, "live-offsets", parent)
    processor = _processor(db, session_manager)
    await _pass(processor, session, parent)
    assert batches[-1] == (["sub-1"], [True])

    _append(subagent, [_usage_line("sub-2", 20, 2)])
    await _pass(processor, session, parent)
    assert batches[-1] == (["sub-2"], [True])

    # A restart rereads the subagent from offset 0 and inserts nothing new.
    await _pass(_processor(db, session_manager), session, parent)
    assert batches[-1] == (["sub-1", "sub-2"], [False, False])
    assert _ids(db, session.id) == ["sub-1", "sub-2"]
    assert _totals(session_manager, session.id) == (30, 3)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subagent_identity_is_stable_across_passes(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [_user_line()])
    subagent = _subagent_file(parent, "a1")
    _write(subagent, [_usage_line(None, 10, 1)])
    session = _register(session_manager, "stable-identity", parent)
    processor = _processor(db, session_manager)
    await _pass(processor, session, parent)

    _append(subagent, [_usage_line(None, 20, 2)])
    await _pass(processor, session, parent)
    ids = _ids(db, session.id)
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert _totals(session_manager, session.id) == (30, 3)

    await _pass(_processor(db, session_manager), session, parent)
    assert _ids(db, session.id) == ids

    await lifecycle_manager._process_session_transcript(session.id, str(parent))
    assert _ids(db, session.id) == ids
    assert _totals(session_manager, session.id) == (30, 3)

    # Truncation below the cursor resets the file to offset 0, so new content is read.
    _write(subagent, [])
    await _pass(processor, session, parent)
    _append(subagent, [_usage_line("sub-short", 5, 1)])
    await _pass(processor, session, parent)
    assert "sub-short" in _ids(db, session.id)

    # Replacement with a new inode also resets the file to offset 0.
    replacement = subagent.with_suffix(".tmp")
    replacement.write_text(f"{_usage_line('sub-replaced', 7, 1)}\n", encoding="utf-8")
    os.replace(replacement, subagent)
    await _pass(processor, session, parent)
    assert "sub-replaced" in _ids(db, session.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subagent_rows_never_touch_occupancy(
    db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [_user_line(), _usage_line("parent-1", 1000, 10)])
    session = _register(session_manager, "occupancy-guard", parent)
    processor = _processor(db, session_manager)
    await _pass(processor, session, parent)
    before = _occupancy(session_manager, session.id)
    assert before[0] is not None

    _write(_subagent_file(parent, "a1"), [_usage_line("sub-big", 90_000, 500)])
    await _pass(processor, session, parent)
    assert _ids(db, session.id) == ["parent-1", "sub-big"]
    assert _occupancy(session_manager, session.id) == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idle_parent_pass_ingests_subagent_appends(
    db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [_user_line(), _usage_line("parent-1", 1000, 10)])
    session = _register(session_manager, "idle-parent", parent)
    processor = _processor(db, session_manager)
    await _pass(processor, session, parent)
    occupancy = _occupancy(session_manager, session.id)
    subagent = _subagent_file(parent, "a1")
    _write(subagent, [])

    # The parent transcript stays unchanged while two complete lines land in the subagent file.
    _append(subagent, [_usage_line("sub-1", 100, 10), _usage_line("sub-2", 200, 20)])
    await _pass(processor, session, parent)
    assert _ids(db, session.id) == ["parent-1", "sub-1", "sub-2"]
    assert _totals(session_manager, session.id) == (1300, 40)
    assert _occupancy(session_manager, session.id) == occupancy

    await _pass(processor, session, parent)
    assert _ids(db, session.id) == ["parent-1", "sub-1", "sub-2"]
    assert _totals(session_manager, session.id) == (1300, 40)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fallback_ids_are_agent_scoped_and_audited(
    db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [_user_line()])
    _write(_subagent_file(parent, "a1"), [_usage_line(None, 10, 1)])
    _write(_subagent_file(parent, "a2"), [_usage_line(None, 20, 2)])
    session = _register(session_manager, "fallback-ids", parent)
    await _pass(_processor(db, session_manager), session, parent)

    rows = _rows(db, session.id)
    agents = {row["message_id"]: row["metadata"]["agent_id"] for row in rows}
    assert len(agents) == 2
    assert sorted(agents.values()) == ["a1", "a2"]

    audit = tokens_cli._audit_session(db, session.id, fix=False)
    assert (audit.stale_rows, audit.missing_rows, audit.usage_mismatch) == (0, 0, False)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_totals_refresh_is_repaired_on_replay(
    db: HubDatabase,
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [_user_line(), _usage_line("parent-1", 1000, 10)])
    session = _register(session_manager, "refresh-replay", parent)
    processor = _processor(db, session_manager)
    await _pass(processor, session, parent)
    subagent = _subagent_file(parent, "a1")
    _write(subagent, [])
    _append(subagent, [_usage_line("sub-1", 100, 10)])

    real_update_usage = session_manager.update_usage
    failures = [psycopg.OperationalError("injected totals failure")]

    def update_usage_failing_once(**kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return real_update_usage(**kwargs)

    monkeypatch.setattr(session_manager, "update_usage", update_usage_failing_once)

    # The idle pass inserts sub-1, the totals refresh fails, and the cursor stays put.
    await _pass(processor, session, parent)
    assert _ids(db, session.id) == ["parent-1", "sub-1"]
    assert _totals(session_manager, session.id) == (1000, 10)

    # The replay dedupes sub-1 to False and must still repair the totals.
    await _pass(processor, session, parent)
    assert _ids(db, session.id) == ["parent-1", "sub-1"]
    assert _totals(session_manager, session.id) == (1100, 20)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unexpected_ledger_fault_does_not_double_count_parent_stats(
    db: HubDatabase,
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [_user_line()])
    session = _register(session_manager, "ledger-fault", parent)
    processor = _processor(db, session_manager)
    await _pass(processor, session, parent)
    before = session_manager.get(session.id)
    assert before is not None
    _write(_subagent_file(parent, "a1"), [_usage_line("sub-1", 100, 10)])
    _append(parent, [_usage_line("parent-1", 1000, 10)])

    faults = [RuntimeError("injected ledger fault")]

    def read_failing_once(*args: Any, **kwargs: Any) -> Any:
        if faults:
            raise faults.pop()
        return read_subagent_events(*args, **kwargs)

    monkeypatch.setattr("gobby.sessions.processor_ledger.read_subagent_events", read_failing_once)

    # A ledger error the ledger does not handle escapes the pass.
    with pytest.raises(RuntimeError, match="injected ledger fault"):
        await processor._process_session(session.id, str(parent))

    # The retry counts the new parent message once, not once per attempt.
    await _pass(processor, session, parent)
    after = session_manager.get(session.id)
    assert after is not None
    assert after.message_count == (before.message_count or 0) + 1
    assert sorted(_ids(db, session.id)) == ["parent-1", "sub-1"]


@pytest.mark.unit
def test_subagent_reader_takes_a_complete_final_record_and_waits_for_a_torn_one(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.jsonl"
    _write(parent, [])
    subagent = _subagent_file(parent, "a1")
    _write(subagent, [])
    record = _usage_line("sub-1", 100, 10)

    # A torn write is not a record yet: nothing is read and the cursor stays at zero.
    subagent.write_text(record[:20], encoding="utf-8")
    events, cursors = read_subagent_events("ledger-reader", PROJECT_ID, str(parent), {})
    assert events == []

    # The writer finishes the record without its newline. Complete JSON is a record at EOF.
    with subagent.open("a", encoding="utf-8") as handle:
        handle.write(record[20:])
    events, cursors = read_subagent_events("ledger-reader", PROJECT_ID, str(parent), cursors)
    assert [event.message_id for event in events] == ["sub-1"]

    # The newline arrives later, and the next record is read once without a duplicate.
    with subagent.open("a", encoding="utf-8") as handle:
        handle.write("\n" + _usage_line("sub-2", 200, 20) + "\n")
    events, cursors = read_subagent_events("ledger-reader", PROJECT_ID, str(parent), cursors)
    assert [event.message_id for event in events] == ["sub-2"]


@pytest.mark.unit
def test_subagent_reader_reads_at_most_one_chunk_per_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A one-byte chunk is smaller than any line, so each pass takes exactly one line.
    monkeypatch.setattr(subagent_usage, "SUBAGENT_CHUNK_BYTES", 1)
    parent = tmp_path / "parent.jsonl"
    _write(parent, [])
    _write(_subagent_file(parent, "a1"), [_usage_line(f"sub-{n}", 10 * n, n) for n in (1, 2, 3)])

    cursors: dict[str, SubagentCursor] = {}
    passes: list[list[str | None]] = []
    for _ in range(3):
        events, cursors = read_subagent_events("ledger-bounds", PROJECT_ID, str(parent), cursors)
        passes.append([event.message_id for event in events])
    assert passes == [["sub-1"], ["sub-2"], ["sub-3"]]
