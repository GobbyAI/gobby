from __future__ import annotations

from typing import Any, Literal

from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.metric_snapshots import MetricSnapshotStorage
from gobby.storage.sql_dialect import (
    elapsed_seconds_greater_than_expr,
    json_text_expr,
    newer_than_now_expr,
    older_than_now_expr,
    timestamp_plus_seconds_before_now_expr,
)
from gobby.storage.token_events import TokenEventStore


class _CaptureDb:
    def __init__(self, dialect: Literal["sqlite", "postgres"]) -> None:
        self.dialect = dialect
        self.queries: list[str] = []
        self.params: list[tuple[Any, ...]] = []

    def fetchone(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.queries.append(query)
        self.params.append(params)
        return None

    def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[Any]:
        self.queries.append(query)
        self.params.append(params)
        return []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        self.queries.append(query)
        self.params.append(params)
        return type("Cursor", (), {"rowcount": 0})()


class _Db:
    def __init__(self, dialect: Literal["sqlite", "postgres"]) -> None:
        self.dialect = dialect


def test_json_text_expr_uses_jsonb_path_operator_for_postgres() -> None:
    assert json_text_expr(_Db("postgres"), "metadata", "skillport", "category") == (
        "metadata #>> '{skillport,category}'"
    )


def test_json_text_expr_preserves_sqlite_json_extract_for_overlap_window() -> None:
    assert json_text_expr(_Db("sqlite"), "metadata", "skillport", "category") == (
        "json_extract(metadata, '$.skillport.category')"
    )


def test_timestamp_helpers_emit_postgres_native_time_arithmetic() -> None:
    db = _Db("postgres")

    assert older_than_now_expr(db, "updated_at", "?", "hour") == (
        "updated_at < NOW() - (? * INTERVAL '1 hour')"
    )
    assert newer_than_now_expr(db, "created_at", "?", "minute") == (
        "created_at >= NOW() - (? * INTERVAL '1 minute')"
    )
    assert timestamp_plus_seconds_before_now_expr(db, "started_at", "timeout_seconds") == (
        "started_at + (timeout_seconds * INTERVAL '1 second') < NOW()"
    )
    assert elapsed_seconds_greater_than_expr(db, "last_activity_at", "timeout_seconds") == (
        "EXTRACT(EPOCH FROM (NOW() - last_activity_at)) > timeout_seconds"
    )


def test_completion_notification_query_uses_postgres_jsonb_without_json_valid() -> None:
    db = _CaptureDb("postgres")
    manager = InterSessionMessageManager(db)  # type: ignore[arg-type]

    assert manager.has_completion_notification("sess", "completion", "run-1") is False

    query = db.queries[-1]
    assert "metadata_json #>> '{completion_id}'" in query
    assert "metadata_json #>> '{run_id}'" in query
    assert "metadata_json #>> '{execution_id}'" in query
    assert "json_valid" not in query


def test_metric_snapshot_queries_use_postgres_intervals() -> None:
    db = _CaptureDb("postgres")
    storage = MetricSnapshotStorage(db)  # type: ignore[arg-type]

    assert storage.get_snapshots(hours=6) == []
    storage.delete_old_snapshots(retention_hours=24)

    assert "timestamp >= NOW() - (? * INTERVAL '1 hour')" in db.queries[0]
    assert db.params[0] == (6, 120)
    assert "timestamp < NOW() - (? * INTERVAL '1 hour')" in db.queries[1]
    assert db.params[1] == (24,)


def test_token_timeseries_uses_postgres_bucket_and_window_sql() -> None:
    db = _CaptureDb("postgres")
    store = TokenEventStore(db)  # type: ignore[arg-type]

    assert store.get_timeseries(hours=12, granularity="30m") == []

    query = db.queries[-1]
    assert "date_trunc('hour', event_at AT TIME ZONE 'UTC')" in query
    assert "EXTRACT(MINUTE FROM event_at AT TIME ZONE 'UTC')" in query
    assert "event_at >= NOW() - (? * INTERVAL '1 hour')" in query
    assert db.params[-1] == (12,)
