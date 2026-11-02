"""Per-call identity and api_calls for the usage ledger (plan 2.1, #23598)."""

import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.cli import tokens as tokens_cli
from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from gobby.sessions.usage_calls import api_call_count
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import TokenEvent, TokenEventStore
from tests.config_runtime_helpers import static_session_capture
from tests.fixtures.postgres import TEST_USER_ID

PROJECT_ID = "00000000-0000-4000-8000-000000000231"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-00000000023a"
TS = "2026-10-20T10:00:00Z"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "Usage Call Identity"),
    )
    LocalMachineManager(hub_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID)
    return hub_db


@pytest.fixture
def session_manager(db: HubDatabase) -> SessionManager:
    return SessionManager(db)


@pytest.fixture
def lifecycle_manager(db: HubDatabase) -> SessionLifecycleManager:
    return SessionLifecycleManager(db, static_session_capture(SessionLifecycleConfig()))


def _register(
    session_manager: SessionManager,
    *,
    external_id: str,
    source: str,
    transcript_path: Path | None = None,
) -> Any:
    return session_manager.register(
        external_id=external_id,
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id=PROJECT_ID,
        title=external_id,
        transcript_path=str(transcript_path) if transcript_path else None,
    )


def _usage(input_tokens: int, cached: int, output: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "total_tokens": input_tokens + output,
    }


def _token_count_line(last: dict[str, int], total: dict[str, int] | None) -> str:
    info: dict[str, Any] = {"last_token_usage": last, "model_context_window": 258400}
    if total is not None:
        info["total_token_usage"] = total
    record = {
        "timestamp": TS,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }
    return json.dumps(record)


def _cumulative_rollout() -> list[str]:
    """Three token_count lines; the third repeats the second's cumulative total."""
    first = _token_count_line(_usage(100, 40, 10), _usage(100, 40, 10))
    second = _token_count_line(_usage(50, 20, 5), _usage(150, 60, 15))
    return [first, second, second]


def _jsonl(lines: Iterable[str]) -> str:
    return "".join(line + "\n" for line in lines)


def _claude_record(message_id: str, input_tokens: int, output_tokens: int, **usage: Any) -> str:
    message_usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    message_usage.update(usage)
    record = {
        "type": "assistant",
        "timestamp": TS,
        "message": {
            "id": message_id,
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi"}],
            "usage": message_usage,
        },
    }
    return json.dumps(record)


def _grok_turn_line() -> str:
    update = {
        "sessionUpdate": "turn_completed",
        "prompt_id": "prompt-1",
        "usage": {
            "inputTokens": 10,
            "cachedInputTokens": 0,
            "cacheCreationTokens": 0,
            "outputTokens": 5,
            "modelCalls": 3,
            "usageIsIncomplete": False,
        },
    }
    return json.dumps({"timestamp": TS, "params": {"update": update}})


def _usage_messages(records: Iterable[Any]) -> list[ParsedMessage]:
    return [r for r in records if isinstance(r, ParsedMessage) and r.usage is not None]


def _parsed(
    content_type: str,
    raw_json: dict[str, Any],
    *,
    message_id: str | None = None,
    usage: TokenUsage | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        index=0,
        role="assistant",
        content="",
        content_type=content_type,
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime(2026, 10, 20, 10, tzinfo=UTC),
        raw_json=raw_json,
        usage=usage or TokenUsage(input_tokens=10, output_tokens=5),
        model=None,
        message_id=message_id,
    )


def _event(session_id: str, message: ParsedMessage, source: str) -> TokenEvent:
    usage = message.usage
    assert usage is not None
    return TokenEvent(
        session_id=session_id,
        project_id=PROJECT_ID,
        message_id=message.message_id,
        source=source,
        origin="transcript",
        model=message.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        event_at=message.timestamp,
    )


def _stored(
    session_id: str,
    *,
    source: str,
    message_id: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    api_calls: int | None = None,
) -> TokenEvent:
    return TokenEvent(
        session_id=session_id,
        project_id=PROJECT_ID,
        message_id=message_id,
        source=source,
        origin="transcript",
        model=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=cache_read,
        event_at=datetime(2026, 10, 20, 10, tzinfo=UTC),
        api_calls=api_calls,
    )


@pytest.mark.integration
def test_codex_cumulative_id_dedupes_repeats(
    db: HubDatabase, session_manager: SessionManager
) -> None:
    session = _register(session_manager, external_id="codex-cumulative", source="codex")
    parser = CodexTranscriptParser(session_id=session.id)
    rollout = _jsonl(_cumulative_rollout()).splitlines(keepends=True)
    messages = _usage_messages(parser.parse_lines(rollout, start_index=0))

    cumulative_key = f"{session.id}:codex:165:150:60:15"
    assert [m.message_id for m in messages] == [
        f"{session.id}:codex:110:100:40:10",
        cumulative_key,
        cumulative_key,
    ]

    store = TokenEventStore(db)
    events = [_event(session.id, m, "codex") for m in messages]
    assert store.record_batch(events) == [True, True, False]
    assert store.get_session_totals(session.id) == {
        "input_tokens": 90,
        "output_tokens": 15,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 60,
    }


@pytest.mark.unit
def test_codex_without_cumulative_keeps_index_id() -> None:
    parser = CodexTranscriptParser(session_id="codex-session")
    line = _token_count_line(_usage(100, 40, 10), total=None)

    [message] = _usage_messages(parser.parse_lines([line + "\n"], start_index=0))

    assert message.message_id == "codex-session:codex:0"


@pytest.mark.unit
def test_api_call_count_per_source() -> None:
    iterated = {
        "message": {
            "usage": {
                "input_tokens": 10,
                "iterations": [
                    {"type": "message"},
                    {"type": "message"},
                    {"type": "advisor_message"},
                ],
            }
        }
    }
    assert api_call_count("claude", _parsed("text", iterated)) == 2

    plain = {"message": {"usage": {"input_tokens": 10}}}
    assert api_call_count("claude", _parsed("text", plain)) == 1

    assert api_call_count("codex", _parsed("usage", {})) == 1

    turn = {
        "params": {
            "update": {
                "sessionUpdate": "turn_completed",
                "usage": {"modelCalls": 4, "usageIsIncomplete": False},
            }
        }
    }
    assert api_call_count("grok", _parsed("turn_completed", turn)) == 4

    incomplete = {
        "params": {
            "update": {
                "sessionUpdate": "turn_completed",
                "usage": {"modelCalls": 4, "usageIsIncomplete": True},
            }
        }
    }
    assert api_call_count("grok", _parsed("turn_completed", incomplete)) is None

    no_count = {
        "params": {
            "update": {"sessionUpdate": "turn_completed", "usage": {"usageIsIncomplete": False}}
        }
    }
    assert api_call_count("grok", _parsed("turn_completed", no_count)) is None

    assert api_call_count("droid", _parsed("text", iterated)) is None
    assert api_call_count("qwen", _parsed("text", iterated)) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_writers_persist_api_calls(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    iterations = [{"type": "message"}, {"type": "message"}]

    live = _register(session_manager, external_id="live-calls", source="claude")
    live_message = _parsed(
        "text",
        {"message": {"usage": {"input_tokens": 10, "iterations": iterations}}},
        message_id="msg-live",
    )
    processor = SessionMessageProcessor(db, session_manager=session_manager)
    await processor._persist_usage_events(live.id, [live_message])

    rebuild_path = tmp_path / "rebuild.jsonl"
    rebuild_path.write_text(
        _jsonl([_claude_record("msg-rebuild", 10, 5, iterations=iterations)]),
        encoding="utf-8",
    )
    rebuild = _register(
        session_manager,
        external_id="rebuild-calls",
        source="claude",
        transcript_path=rebuild_path,
    )
    await lifecycle_manager._process_session_transcript(rebuild.id, str(rebuild_path))

    store = TokenEventStore(db)
    live_rows = {row["message_id"]: row["api_calls"] for row in store.list_session_events(live.id)}
    rebuild_rows = {
        row["message_id"]: row["api_calls"] for row in store.list_session_events(rebuild.id)
    }
    assert live_rows == {"msg-live": 2}
    assert rebuild_rows == {"msg-rebuild": 2}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_dedupes_and_flags_unkeyed_rows(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_jsonl(_cumulative_rollout()), encoding="utf-8")
    session = _register(
        session_manager, external_id="codex-audit", source="codex", transcript_path=rollout
    )
    await lifecycle_manager._process_session_transcript(session.id, str(rollout))
    store = TokenEventStore(db)

    clean = tokens_cli._audit_session(db, session.id, fix=False)
    assert (clean.stale_rows, clean.missing_rows, clean.usage_mismatch) == (0, 0, False)

    # One missing key, not two: the repeated total derives a single row.
    db.execute(
        "DELETE FROM token_events WHERE session_id = %s AND message_id = %s",
        (session.id, f"{session.id}:codex:165:150:60:15"),
    )
    missing = tokens_cli._audit_session(db, session.id, fix=False)
    assert (missing.stale_rows, missing.missing_rows) == (0, 1)

    store.record(
        _stored(session.id, source="codex", message_id=None, input_tokens=1, output_tokens=1)
    )
    # A row with no message_id matches no transcript key, so it is drift.
    unkeyed = tokens_cli._audit_session(db, session.id, fix=False)
    assert unkeyed.stale_rows == 1

    repaired = tokens_cli._audit_session(db, session.id, fix=True)
    assert repaired.repaired
    final = tokens_cli._audit_session(db, session.id, fix=False)
    assert (final.stale_rows, final.missing_rows, final.usage_mismatch) == (0, 0, False)
    assert all(row["message_id"] is not None for row in store.list_session_events(session.id))


@pytest.mark.integration
def test_audit_flags_equal_total_identity_drift(
    db: HubDatabase, session_manager: SessionManager, tmp_path: Path
) -> None:
    store = TokenEventStore(db)

    claude_path = tmp_path / "claude.jsonl"
    claude = _register(
        session_manager, external_id="claude-drift", source="claude", transcript_path=claude_path
    )
    claude_path.write_text(
        _jsonl([_claude_record("msg-claude", 10, 5, iterations=[{"type": "message"}] * 2)]),
        encoding="utf-8",
    )
    store.record(
        _stored(
            claude.id,
            source="claude",
            message_id="msg-claude",
            input_tokens=10,
            output_tokens=5,
        )
    )

    grok_path = tmp_path / "grok.jsonl"
    grok = _register(
        session_manager, external_id="grok-drift", source="grok", transcript_path=grok_path
    )
    grok_path.write_text(_jsonl([_grok_turn_line()]), encoding="utf-8")
    [grok_message] = _usage_messages(
        GrokTranscriptParser(session_id=grok.id).parse_lines(
            [_grok_turn_line() + "\n"], start_index=0
        )
    )
    store.record(
        _stored(
            grok.id,
            source="grok",
            message_id=grok_message.message_id,
            input_tokens=10,
            output_tokens=5,
        )
    )

    codex_path = tmp_path / "codex.jsonl"
    codex = _register(
        session_manager, external_id="codex-drift", source="codex", transcript_path=codex_path
    )
    codex_path.write_text(_jsonl(_cumulative_rollout()[:2]), encoding="utf-8")
    store.record(
        _stored(
            codex.id,
            source="codex",
            message_id=f"{codex.id}:codex:0",
            input_tokens=60,
            output_tokens=10,
            cache_read=40,
        )
    )
    store.record(
        _stored(
            codex.id,
            source="codex",
            message_id=f"{codex.id}:codex:1",
            input_tokens=30,
            output_tokens=5,
            cache_read=20,
        )
    )

    sessions = (claude.id, grok.id, codex.id)
    first_pass = [tokens_cli._audit_session(db, sid, fix=False) for sid in sessions]
    assert [a.stale_rows for a in first_pass] == [1, 1, 2]

    claude_row_id = _row_id(store, claude.id, "msg-claude")
    for sid in sessions:
        assert tokens_cli._audit_session(db, sid, fix=True).repaired

    second_pass = [tokens_cli._audit_session(db, sid, fix=False) for sid in sessions]
    assert [(a.stale_rows, a.missing_rows, a.usage_mismatch) for a in second_pass] == [
        (0, 0, False)
    ] * 3
    assert _row_id(store, claude.id, "msg-claude") == claude_row_id
    assert {r["message_id"]: r["api_calls"] for r in store.list_session_events(claude.id)} == {
        "msg-claude": 2
    }
    assert [r["api_calls"] for r in store.list_session_events(grok.id)] == [3]


@pytest.mark.integration
def test_fix_never_deletes_rows_ingested_during_audit(
    db: HubDatabase,
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = TokenEventStore(db)
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(_jsonl([_claude_record("msg-a", 10, 5)]), encoding="utf-8")
    session = _register(
        session_manager, external_id="fix-safety", source="claude", transcript_path=transcript
    )
    # Pre-read rows: a keyed row with NULL api_calls, and an unkeyed row.
    store.record(
        _stored(session.id, source="claude", message_id="msg-a", input_tokens=10, output_tokens=5)
    )
    store.record(
        _stored(session.id, source="claude", message_id=None, input_tokens=1, output_tokens=1)
    )
    pre_read_ids = {r["message_id"]: r["id"] for r in store.list_session_events(session.id)}

    original_read = tokens_cli._read_stored_rows
    original_load = tokens_cli._load_session_messages

    def read_then_ingest(db_: HubDatabase, session_id: str) -> list[Mapping[str, Any]]:
        rows = original_read(db_, session_id)
        # The live processor ingests a line after the stored-row read, before the transcript read.
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_jsonl([_claude_record("msg-live", 7, 3)]))
        store.record(
            _stored(
                session.id,
                source="claude",
                message_id="msg-live",
                input_tokens=7,
                output_tokens=3,
                api_calls=1,
            )
        )
        return rows

    def load_then_append(session_id: str, session_row: Any) -> list[Any]:
        messages = original_load(session_id, session_row)
        # A line appended after the transcript read belongs to the next audit pass.
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_jsonl([_claude_record("msg-after", 2, 1)]))
        return messages

    monkeypatch.setattr(tokens_cli, "_read_stored_rows", read_then_ingest)
    monkeypatch.setattr(tokens_cli, "_load_session_messages", load_then_append)

    first = tokens_cli._audit_session(db, session.id, fix=True)
    assert first.repaired
    assert first.stale_rows == 2  # msg-a with NULL api_calls, and the unkeyed row
    assert first.missing_rows == 1  # msg-live: ingested after the stored-row read

    monkeypatch.undo()
    rows = {r["message_id"]: r for r in store.list_session_events(session.id)}
    assert None not in rows
    assert rows["msg-a"]["id"] == pre_read_ids["msg-a"]
    assert rows["msg-a"]["api_calls"] == 1
    assert rows["msg-live"]["input_tokens"] == 7
    assert "msg-after" not in rows

    refreshed = session_manager.get(session.id)
    totals = store.get_session_totals(session.id)
    assert refreshed is not None
    assert refreshed.usage_input_tokens == totals["input_tokens"]
    assert refreshed.usage_output_tokens == totals["output_tokens"]

    second = tokens_cli._audit_session(db, session.id, fix=False)
    assert (second.stale_rows, second.missing_rows, second.usage_mismatch) == (0, 1, False)


def _row_id(store: TokenEventStore, session_id: str, message_id: str) -> Any:
    return next(
        r["id"] for r in store.list_session_events(session_id) if r["message_id"] == message_id
    )
