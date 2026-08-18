from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from gobby.mcp_proxy.metrics_events import MetricsEventStore
from gobby.mcp_proxy.metrics_store import ToolMetricsStore
from gobby.mcp_proxy.schema_hash import SchemaHashManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
from gobby.storage.cron_runs import CronRunStorageMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories_crossrefs import MemoryCrossRefMixin
from gobby.storage.memories_dreams import MemoryDreamMixin
from gobby.storage.memories_query import MemoryQueryMixin
from gobby.storage.memories_scope import ALL_MEMORIES
from gobby.storage.sessions._usage import _UsageMixin
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit


@dataclass
class _RecordedCall:
    sql: str
    params: Sequence[Any] | Mapping[str, Any]


class _RecordingCursor:
    def __init__(self, row: Mapping[str, Any] | None = None, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self) -> Mapping[str, Any] | None:
        return self._row

    def fetchall(self) -> list[Mapping[str, Any]]:
        return []


class _RecordingTransaction:
    def __init__(self, db: _RecordingDB) -> None:
        self._db = db

    def __enter__(self) -> _RecordingTransaction:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> _RecordingCursor:
        return self._db.execute(sql, params)


class _RecordingDB:
    dialect = "postgres"

    def __init__(
        self,
        *,
        fetchone_row: Mapping[str, Any] | None = None,
        execute_row: Mapping[str, Any] | None = None,
    ) -> None:
        self.calls: list[_RecordedCall] = []
        self._fetchone_row = fetchone_row
        self._execute_row = execute_row

    def transaction(self) -> _RecordingTransaction:
        return _RecordingTransaction(self)

    def transaction_immediate(self, lock: object | None = None) -> _RecordingTransaction:
        return _RecordingTransaction(self)

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> _RecordingCursor:
        self.calls.append(_RecordedCall(sql, params))
        if "RETURNING sequence" in sql:
            # Memory writers append to the embedding projection ledger inside
            # the same transaction; the insert reads back its sequence.
            return _RecordingCursor(row={"sequence": 1}, rowcount=1)
        return _RecordingCursor(row=self._execute_row, rowcount=1)

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Mapping[str, Any] | None:
        self.calls.append(_RecordedCall(sql, params))
        return self._fetchone_row

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Mapping[str, Any]]:
        self.calls.append(_RecordedCall(sql, params))
        return []


class _UsageRecorder(_UsageMixin):
    def __init__(self, db: _RecordingDB) -> None:
        self.db: HubDatabase = cast(HubDatabase, db)


class _CronRecorder(CronRunStorageMixin):
    def __init__(self, db: _RecordingDB) -> None:
        self.db: HubDatabase = cast(HubDatabase, db)


def _params(call: _RecordedCall) -> Sequence[Any]:
    assert not isinstance(call.params, Mapping)
    return call.params


def _assert_aware_utc(value: object) -> datetime:
    assert isinstance(value, datetime)
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)
    return value


def test_task_mutex_acquire_binds_aware_datetime_values() -> None:
    db = _RecordingDB()
    manager = TaskDispatchMutexManager(db)  # type: ignore[arg-type]
    now = "2026-07-03T01:02:03+00:00"

    acquired = manager.acquire_mutex(
        task_id="task-1",
        holder="session-1",
        kind="dispatch",
        ttl_seconds=30,
        run_id="run-1",
        now=now,
    )

    assert acquired is True
    params = _params(db.calls[-1])
    lease_until = _assert_aware_utc(params[1])
    updated_at = _assert_aware_utc(params[5])
    assert lease_until == updated_at + timedelta(seconds=30)


def test_session_context_usage_binds_snapshot_timestamp_as_datetime() -> None:
    db = _RecordingDB()
    manager = _UsageRecorder(db)
    snapshot = ContextUsageSnapshot(
        source="codex",
        model="gpt-5",
        context_window=100_000,
        context_used_tokens=25_000,
        context_usage_ratio=0.25,
        confidence="reported",
        timestamp=datetime(2026, 7, 3, 1, 2, 3, tzinfo=UTC),
    )

    updated = manager.update_context_usage("session-1", snapshot)

    assert updated is True
    params = _params(db.calls[-1])
    _assert_aware_utc(params[5])


def test_memory_timestamp_writers_and_filters_bind_datetimes() -> None:
    # restore_memory SELECTs the row under FOR UPDATE before writing; give the
    # execute-cursor a row so the existence check passes.
    db = _RecordingDB(
        execute_row={
            "vector_needs_reindex": False,
            "created_at": datetime(2026, 7, 3, 1, 2, 3, tzinfo=UTC),
        }
    )
    crossrefs = MemoryCrossRefMixin(db)  # type: ignore[arg-type]
    access = MemoryQueryMixin(db)  # type: ignore[arg-type]
    dreams = MemoryDreamMixin(db)  # type: ignore[arg-type]
    timestamp = "2026-07-03T01:02:03+00:00"

    crossref = crossrefs.create_crossref("source", "target", 0.9)
    _assert_aware_utc(crossref.created_at)

    access.update_access_stats("memory-1", timestamp)
    _assert_aware_utc(_params(db.calls[-1])[0])

    dreams.mark_dreamed("memory-1", hidden_as="review", when=timestamp)
    # The visibility write is followed by the projection-ledger advisory lock
    # and append in the same transaction; the memory UPDATE is third-last.
    dreamed_params = _params(db.calls[-3])
    _assert_aware_utc(dreamed_params[0])
    _assert_aware_utc(dreamed_params[1])

    dreams.restore_memory("memory-1", when=timestamp)
    _assert_aware_utc(_params(db.calls[-3])[0])

    dreams.list_dream_candidates(limit=10, redream_cutoff=timestamp, scope=ALL_MEMORIES)
    _assert_aware_utc(_params(db.calls[-1])[0])

    dreams.list_dream_scopes(redream_cutoff=timestamp)
    _assert_aware_utc(_params(db.calls[-1])[0])


def test_memory_access_stats_rejects_invalid_accessed_at_before_transaction() -> None:
    db = _RecordingDB()
    access = MemoryQueryMixin(db)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Invalid isoformat"):
        access.update_access_stats("memory-1", "not-a-timestamp")

    assert db.calls == []


def test_cron_run_create_binds_triggered_and_created_at_as_datetimes() -> None:
    db = _RecordingDB()
    storage = _CronRecorder(db)

    run = storage.create_run("cron-job-1")

    assert run is None
    params = _params(db.calls[-1])
    _assert_aware_utc(params[3])


def test_schema_hash_manager_binds_verification_timestamps_as_datetimes() -> None:
    db = _RecordingDB(
        fetchone_row={
            "id": 1,
            "server_name": "server",
            "tool_name": "tool",
            "project_id": "project",
            "schema_hash": "hash",
            "last_verified_at": "2026-07-03T01:02:03+00:00",
            "created_at": "2026-07-03T01:02:03+00:00",
            "updated_at": "2026-07-03T01:02:03+00:00",
        }
    )
    manager = SchemaHashManager(db)  # type: ignore[arg-type]

    manager.store_hash("server", "tool", "project", "hash")
    store_params = _params(db.calls[0])
    _assert_aware_utc(store_params[4])

    updated = manager.update_verification_time("server", "tool", "project")
    assert updated is True
    update_params = _params(db.calls[-1])
    _assert_aware_utc(update_params[0])
    _assert_aware_utc(update_params[1])


def test_tool_metrics_store_binds_datetimes_for_writes_and_cutoffs() -> None:
    db = _RecordingDB()
    store = ToolMetricsStore(db)  # type: ignore[arg-type]

    store.record_call("server", "tool", "project", latency_ms=12.5, success=True)
    record_params = _params(db.calls[-1])
    for index in (8, 13, 14):
        _assert_aware_utc(record_params[index])

    store.aggregate_to_daily(retention_days=7)
    _assert_aware_utc(_params(db.calls[-1])[0])

    cutoff = datetime(2026, 7, 5, 1, 2, 3, tzinfo=UTC)
    store.cleanup_old_metrics(cutoff)
    cleanup_params = _params(db.calls[-1])
    assert _assert_aware_utc(cleanup_params[0]) == cutoff


def test_metrics_event_store_binds_datetimes_for_filters_and_archive() -> None:
    db = _RecordingDB()
    store = MetricsEventStore(db)  # type: ignore[arg-type]
    since = datetime(2026, 7, 3, 1, 2, 3, tzinfo=UTC)
    until = datetime(2026, 7, 4, 1, 2, 3, tzinfo=UTC)

    store.get_rule_stats(since=since)
    _assert_aware_utc(_params(db.calls[-1])[0])

    store.get_skill_stats(since=since)
    _assert_aware_utc(_params(db.calls[-1])[0])

    store.query_events(since=since, until=until)
    query_params = _params(db.calls[-1])
    _assert_aware_utc(query_params[0])
    _assert_aware_utc(query_params[1])

    store.get_timeseries("rule_eval", range_key="1h")
    _assert_aware_utc(_params(db.calls[-1])[1])

    store.archive_old_events(retention_days=30)
    archive_params = _params(db.calls[-1])
    _assert_aware_utc(archive_params[0])


def test_worktree_and_clone_create_bind_and_return_datetimes() -> None:
    returned = {
        "created_at": datetime(2026, 7, 3, 1, 2, 3, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 3, 1, 2, 3, tzinfo=UTC),
    }
    worktree_db = _RecordingDB(execute_row=returned)
    worktree = LocalWorktreeManager(worktree_db)  # type: ignore[arg-type]

    worktree_model = worktree.create(
        project_id="project",
        branch_name="feature/worktree",
        worktree_path="/tmp/worktree",
    )

    worktree_params = _params(worktree_db.calls[-1])
    _assert_aware_utc(worktree_params[9])
    _assert_aware_utc(worktree_model.created_at)
    _assert_aware_utc(worktree_model.updated_at)

    clone_db = _RecordingDB(execute_row=returned)
    clone = LocalCloneManager(clone_db)  # type: ignore[arg-type]
    cleanup_after = "2026-07-04T01:02:03+00:00"

    clone_model = clone.create(
        project_id="project",
        branch_name="feature/clone",
        clone_path="/tmp/clone",
        cleanup_after=cleanup_after,
    )

    clone_params = _params(clone_db.calls[-1])
    _assert_aware_utc(clone_params[11])
    assert _assert_aware_utc(clone_model.cleanup_after) == datetime(2026, 7, 4, 1, 2, 3, tzinfo=UTC)
    _assert_aware_utc(clone_model.created_at)
    _assert_aware_utc(clone_model.updated_at)

    clone.update("clone-1", cleanup_after=cleanup_after)
    # update() re-reads the row afterwards with two SELECTs (machine-scoped
    # read plus the cross-machine fallback probe); the UPDATE is third-last.
    update_params = _params(clone_db.calls[-3])
    _assert_aware_utc(update_params[0])
    _assert_aware_utc(update_params[1])
