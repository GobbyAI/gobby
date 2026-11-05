"""UsageLedgerStore: scopes, paging, rebuilds, attribution and coverage (plan 3.1, #23601)."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.reported_usage import ReportedRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.reported_usage import ReportedUsageStore
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.token_events import TokenEvent, TokenEventStore
from gobby.storage.usage_ledger import UsageLedgerStore
from gobby.utils.machine_id import get_machine_id
from tests.config_runtime_helpers import static_session_capture
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.integration

BASE = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
TS = "2026-10-20T10:00:00Z"
APPENDED_TS = "2026-10-20T10:10:00Z"


def _at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def _machine_id(db: HubDatabase) -> str:
    machine_id = get_machine_id()
    assert machine_id is not None
    LocalMachineManager(db).upsert_seen(machine_id, TEST_USER_ID)
    return machine_id


@pytest.fixture
def session_manager(temp_db: HubDatabase) -> SessionManager:
    return SessionManager(temp_db)


@pytest.fixture
def lifecycle_manager(temp_db: HubDatabase) -> SessionLifecycleManager:
    return SessionLifecycleManager(temp_db, static_session_capture(SessionLifecycleConfig()))


def _session(
    session_manager: SessionManager,
    project: dict[str, Any],
    name: str,
    source: str = "claude",
    transcript_path: Path | None = None,
) -> str:
    return session_manager.register(
        external_id=f"usage-ledger-{name}",
        machine_id=_machine_id(session_manager.db),
        source=source,
        project_id=project["id"],
        transcript_path=str(transcript_path) if transcript_path else None,
    ).id


def _task(db: HubDatabase, project: dict[str, Any], title: str) -> str:
    return (
        LocalTaskManager(db)
        .create_task(project["id"], title, validation_criteria="Scope follows.")
        .id
    )


def _interval(
    db: HubDatabase,
    task_id: str,
    session_id: str,
    claimed_at: datetime,
    released_at: datetime | None,
) -> None:
    db.execute(
        """
        INSERT INTO task_claim_intervals (task_id, session_id, claimed_at, released_at)
        VALUES (%s, %s, %s, %s)
        """,
        (task_id, session_id, claimed_at, released_at),
    )


def _event(
    db: HubDatabase,
    project: dict[str, Any],
    session_id: str,
    *,
    at: datetime,
    message_id: str | None,
    source: str = "claude",
    input_tokens: int = 10,
    output_tokens: int = 5,
    api_calls: int | None = 1,
    agent_id: str | None = None,
) -> None:
    TokenEventStore(db).record(
        TokenEvent(
            session_id=session_id,
            project_id=project["id"],
            message_id=message_id,
            source=source,
            origin="transcript",
            model=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            event_at=at,
            api_calls=api_calls,
            metadata={"agent_id": agent_id} if agent_id else None,
        )
    )


def _run(
    run_key: str,
    *,
    source: str,
    started: datetime,
    observed: datetime,
    amount: str | None = None,
    complete: bool = True,
) -> ReportedRun:
    return ReportedRun(
        run_key=run_key,
        source=source,
        cost_unit=None if amount is None else "usd",
        cost_amount=None if amount is None else Decimal(amount),
        cost_complete=complete,
        api_duration_ms=None,
        wall_duration_ms=None,
        started_at=started,
        observed_at=observed,
    )


def _claude_record(
    message_id: str, input_tokens: int, output_tokens: int, timestamp: str = TS
) -> str:
    record = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "id": message_id,
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi"}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    }
    return json.dumps(record)


def _codex_usage(input_tokens: int, cached: int, output: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "total_tokens": input_tokens + output,
    }


def _token_count_line(last: dict[str, int], total: dict[str, int]) -> str:
    record = {
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": last,
                "total_token_usage": total,
                "model_context_window": 258400,
            },
        },
    }
    return json.dumps(record)


def _write_jsonl(path: Path, lines: Iterable[str]) -> Path:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _traverse(
    store: UsageLedgerStore, cursor: str | None = None, limit: int = 1, **scope: Any
) -> list[dict[str, Any]]:
    """Every page from cursor to the end, in order."""
    pages: list[dict[str, Any]] = []
    while True:
        page = store.page(cursor=cursor, limit=limit, **scope)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def _observed(store: UsageLedgerStore, session_id: str, source: str) -> dict[str, Any]:
    page = store.page(session_id=session_id, limit=200)
    descriptors = {entry["source"]: entry for entry in page["coverage"]}
    observed: dict[str, Any] = descriptors[source]["spend_observed"]
    return observed


@dataclass(frozen=True)
class _Mixed:
    """The 3.1.1 fixture after ingestion: one task scope over a Claude and a Codex session."""

    task_id: str
    claude: str
    codex: str
    claude_path: Path
    codex_path: Path


def _claude_lines() -> list[str]:
    # msg-a is written as two content-block lines that share one message.id.
    return [
        _claude_record("msg-a", 10, 5),
        _claude_record("msg-a", 10, 5),
        _claude_record("msg-b", 20, 7),
    ]


def _codex_lines() -> list[str]:
    # The third line repeats the second line's cumulative total.
    return [
        _token_count_line(_codex_usage(100, 40, 10), _codex_usage(100, 40, 10)),
        _token_count_line(_codex_usage(50, 20, 5), _codex_usage(150, 60, 15)),
        _token_count_line(_codex_usage(50, 20, 5), _codex_usage(150, 60, 15)),
    ]


async def _ingest_mixed(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    project: dict[str, Any],
    tmp_path: Path,
) -> _Mixed:
    """Ingest the 3.1.1 transcripts through the real parsers; claim both sessions for one task."""
    claude_path = _write_jsonl(tmp_path / "claude.jsonl", _claude_lines())
    codex_path = _write_jsonl(tmp_path / "codex.jsonl", _codex_lines())
    claude = _session(session_manager, project, "mixed-claude", "claude", claude_path)
    codex = _session(session_manager, project, "mixed-codex", "codex", codex_path)
    await lifecycle_manager._process_session_transcript(claude, str(claude_path))
    await lifecycle_manager._process_session_transcript(codex, str(codex_path))

    task_id = _task(temp_db, project, "Mixed fixture scope")
    _interval(temp_db, task_id, claude, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    _interval(temp_db, task_id, codex, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    return _Mixed(task_id, claude, codex, claude_path, codex_path)


@pytest.mark.asyncio
async def test_fixture_calls_are_counted_once(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    mixed = await _ingest_mixed(
        temp_db, session_manager, lifecycle_manager, sample_project, tmp_path
    )

    totals = UsageLedgerStore(temp_db).page(task_id=mixed.task_id, limit=1000)["totals"]

    # Two distinct Claude calls (msg-a is written twice) and two distinct Codex calls.
    # Codex splits cached input out of input_tokens: call one is 60 input and 40 cached,
    # call two is 30 input and 20 cached.
    assert totals["rows"] == 4
    assert totals["input_tokens"] == 10 + 20 + 60 + 30
    assert totals["cache_read_tokens"] == 40 + 20
    assert totals["output_tokens"] == 5 + 7 + 10 + 5
    assert totals["by_source"]["claude"]["rows"] == 2
    assert totals["by_source"]["codex"]["rows"] == 2


@pytest.mark.asyncio
async def test_paging_neither_loses_nor_double_counts(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    mixed = await _ingest_mixed(
        temp_db, session_manager, lifecycle_manager, sample_project, tmp_path
    )
    store = UsageLedgerStore(temp_db)
    single = store.page(task_id=mixed.task_id, limit=1000)
    pages = _traverse(store, limit=1, task_id=mixed.task_id)

    assert len(pages) == 4
    assert [row for page in pages for row in page["rows"]] == single["rows"]
    assert all(page["totals"] == single["totals"] for page in pages)
    # All four rows share one event_at, so the order falls to session_id, then message key.
    assert len({row["event_at"] for row in single["rows"]}) == 1
    assert {row["session_id"] for row in single["rows"]} == {mixed.claude, mixed.codex}
    assert single["rows"] == sorted(
        single["rows"], key=lambda row: (row["session_id"], row["message_id"])
    )


@pytest.mark.asyncio
async def test_rebuild_keeps_rows_totals_and_cursor(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    mixed = await _ingest_mixed(
        temp_db, session_manager, lifecycle_manager, sample_project, tmp_path
    )
    store = UsageLedgerStore(temp_db)
    before = store.page(task_id=mixed.task_id, limit=1000)
    # The cursor after each row, issued before the rebuild; the last one is NULL.
    cursors = [page["next_cursor"] for page in _traverse(store, limit=1, task_id=mixed.task_id)]
    assert cursors[-1] is None
    assert all(cursor is not None for cursor in cursors[:-1])

    await lifecycle_manager._process_session_transcript(mixed.claude, str(mixed.claude_path))
    await lifecycle_manager._process_session_transcript(mixed.codex, str(mixed.codex_path))

    after = store.page(task_id=mixed.task_id, limit=1000)
    assert after["rows"] == before["rows"]
    assert after["totals"] == before["totals"]
    # Codex message ids come from transcript line indexes, so they survive the delete and reinsert.
    codex_ids = [row["message_id"] for row in after["rows"] if row["source"] == "codex"]
    assert codex_ids == [row["message_id"] for row in before["rows"] if row["source"] == "codex"]
    for index, cursor in enumerate(cursors[:-1]):
        resumed = store.page(task_id=mixed.task_id, cursor=cursor, limit=1)
        assert resumed["rows"] == [before["rows"][index + 1]]


def test_task_scope_latest_claim_wins(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _session(session_manager, sample_project, "handoff")
    first_task = _task(temp_db, sample_project, "First claim")
    second_task = _task(temp_db, sample_project, "Second claim")
    # The first task is claimed 10:00 to 10:30. The second claims 10:20 to 10:50, so the
    # overlap 10:20 to 10:30 belongs to the later claim.
    _interval(temp_db, first_task, session, _at(0), _at(30))
    _interval(temp_db, second_task, session, _at(20), _at(50))

    _event(temp_db, sample_project, session, at=_at(5), message_id="before-overlap")
    _event(temp_db, sample_project, session, at=_at(25), message_id="overlap")
    _event(temp_db, sample_project, session, at=_at(40), message_id="second-only")
    # 10:01 to 10:04 lies wholly inside the first claim. 10:05 to 10:25 spans the second
    # claim's opening at 10:20, so it belongs to neither task.
    ReportedUsageStore(temp_db).upsert_runs(
        session,
        [
            _run("inside-first", source="claude", started=_at(1), observed=_at(4), amount="0.5"),
            _run("spans-both", source="claude", started=_at(5), observed=_at(25), amount="1.0"),
        ],
    )

    store = UsageLedgerStore(temp_db)
    first = store.page(task_id=first_task, limit=1000)
    second = store.page(task_id=second_task, limit=1000)

    assert [row["message_id"] for row in first["rows"]] == ["before-overlap"]
    assert [row["message_id"] for row in second["rows"]] == ["overlap", "second-only"]
    assert first["attribution_since"] == _at(0).isoformat()
    assert second["attribution_since"] == _at(20).isoformat()

    assert first["spend"]["reported"]["runs"] == 1
    assert Decimal(first["spend"]["reported"]["by_unit"]["usd"]) == Decimal("0.5")
    assert [run["run_key"] for run in first["spend"]["unattributed"]] == ["spans-both"]
    assert [run["run_key"] for run in second["spend"]["unattributed"]] == ["spans-both"]
    assert second["spend"]["reported"]["runs"] == 0


def test_coverage_reports_missing_data(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _session(session_manager, sample_project, "coverage", "codex")
    _event(temp_db, sample_project, session, at=_at(0), message_id="codex-1", source="codex")
    _event(
        temp_db,
        sample_project,
        session,
        at=_at(1),
        message_id="droid-1",
        source="droid",
        api_calls=None,
    )
    _event(temp_db, sample_project, session, at=_at(2), message_id=None, source="claude")

    page = UsageLedgerStore(temp_db).page(session_id=session, limit=200)
    descriptors = {entry["source"]: entry for entry in page["coverage"]}

    assert descriptors["codex"]["spend"]["mode"] == "unknown"
    assert descriptors["codex"]["spend"]["reason"]
    assert descriptors["codex"]["spend_observed"]["mode"] == "unknown"
    assert (
        descriptors["codex"]["spend_observed"]["reason"] == descriptors["codex"]["spend"]["reason"]
    )
    assert page["totals"]["api_calls_complete"] is False
    assert page["totals"]["by_source"]["droid"]["api_calls_complete"] is False
    assert descriptors["claude"]["unkeyed_rows"] == 1
    assert descriptors["codex"]["unkeyed_rows"] == 0


def test_spend_unknown_is_distinct_from_reported_zero(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    claude = _session(session_manager, sample_project, "no-cost-state")
    droid = _session(session_manager, sample_project, "no-sidecar", "droid")
    grok_zero = _session(session_manager, sample_project, "grok-zero", "grok")
    grok_incomplete = _session(session_manager, sample_project, "grok-incomplete", "grok")

    _event(temp_db, sample_project, claude, at=_at(0), message_id="c-1")
    _event(temp_db, sample_project, droid, at=_at(0), message_id="d-1", source="droid")
    _event(temp_db, sample_project, grok_zero, at=_at(0), message_id="g-1", source="grok")
    _event(temp_db, sample_project, grok_incomplete, at=_at(0), message_id="g-2", source="grok")
    reported = ReportedUsageStore(temp_db)
    reported.upsert_runs(
        grok_zero,
        [_run("turn-1", source="grok", started=_at(0), observed=_at(0), amount="0")],
    )
    reported.upsert_runs(
        grok_incomplete,
        [_run("turn-1", source="grok", started=_at(0), observed=_at(0), complete=False)],
    )

    store = UsageLedgerStore(temp_db)
    zero = _observed(store, grok_zero, "grok")
    incomplete = _observed(store, grok_incomplete, "grok")

    assert _observed(store, claude, "claude") == {"mode": "unknown", "reason": "no reported run"}
    assert _observed(store, droid, "droid") == {"mode": "unknown", "reason": "no reported run"}
    assert zero["mode"] == "reported"
    assert zero["runs"] == 1
    assert zero["incomplete_runs"] == 0
    assert Decimal(zero["amounts"]["usd"]) == Decimal("0")
    assert incomplete["mode"] == "reported"
    assert incomplete["runs"] == 1
    assert incomplete["incomplete_runs"] == 1
    assert "usd" not in incomplete["amounts"]


@pytest.mark.asyncio
async def test_paging_is_a_live_traversal(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    mixed = await _ingest_mixed(
        temp_db, session_manager, lifecycle_manager, sample_project, tmp_path
    )
    store = UsageLedgerStore(temp_db)
    first = store.page(task_id=mixed.task_id, limit=1)
    assert first["totals"]["rows"] == 4
    cursor = first["next_cursor"]
    assert cursor is not None

    # An appended Claude call arrives through the parser and is dated after the fixture.
    appended = _claude_record("msg-appended", 30, 9, timestamp=APPENDED_TS)
    _write_jsonl(mixed.claude_path, [*_claude_lines(), appended])
    await lifecycle_manager._process_session_transcript(mixed.claude, str(mixed.claude_path))
    # A subagent row dated before the cursor counts in the totals but never on a later page.
    _event(
        temp_db,
        sample_project,
        mixed.claude,
        at=_at(-5),
        message_id="msg-subagent",
        agent_id="agent-1",
    )

    second = store.page(task_id=mixed.task_id, cursor=cursor, limit=1)
    assert second["totals"]["rows"] == 6
    assert second["totals"]["rows"] > first["totals"]["rows"]

    full = store.page(task_id=mixed.task_id, limit=1000)["rows"]
    assert [row["message_id"] for row in full[:2]] == [
        "msg-subagent",
        first["rows"][0]["message_id"],
    ]
    assert full[-1]["message_id"] == "msg-appended"
    later = [
        row
        for page in _traverse(store, cursor=cursor, limit=1, task_id=mixed.task_id)
        for row in page["rows"]
    ]
    assert later == full[2:]
    assert "msg-subagent" not in [row["message_id"] for row in later]

    restarted = [
        row for page in _traverse(store, limit=1, task_id=mixed.task_id) for row in page["rows"]
    ]
    assert restarted == full


def test_malformed_cursor_raises_value_error(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _session(session_manager, sample_project, "bad-cursor")
    _event(temp_db, sample_project, session, at=_at(0), message_id="msg-0")
    not_a_cursor = base64.urlsafe_b64encode(b'{"not": "a cursor"}').decode()

    store = UsageLedgerStore(temp_db)
    with pytest.raises(ValueError):
        store.page(session_id=session, cursor="%%%not-base64")
    with pytest.raises(ValueError):
        store.page(session_id=session, cursor=not_a_cursor)


def test_scope_must_be_exactly_one(temp_db: HubDatabase) -> None:
    store = UsageLedgerStore(temp_db)
    with pytest.raises(ValueError):
        store.page()
    with pytest.raises(ValueError):
        store.page(session_id="00000000-0000-4000-8000-000000000001", task_id="x")
