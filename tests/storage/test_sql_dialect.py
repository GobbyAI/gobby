from __future__ import annotations

from typing import Any, Literal

import pytest

from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.metric_snapshots import MetricSnapshotStorage
from gobby.storage.skills import LocalSkillManager
from gobby.storage.sql_dialect import (
    elapsed_seconds_greater_than_expr,
    json_array_contains_condition,
    json_text_expr,
    newer_than_now_expr,
    older_than_now_expr,
    table_column_names,
    timestamp_plus_seconds_before_now_expr,
)
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.token_events import TokenEventStore


class _CaptureDb:
    def __init__(self, dialect: Literal["postgres"] = "postgres") -> None:
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
        return type(
            "Cursor",
            (),
            {
                "rowcount": 0,
                "fetchone": lambda _self: None,
                "fetchall": lambda _self: [],
            },
        )()

    def transaction(self) -> _CaptureDb:
        return self

    def __enter__(self) -> _CaptureDb:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _Db:
    def __init__(self, dialect: Literal["postgres"] = "postgres") -> None:
        self.dialect = dialect


def test_json_text_expr_uses_jsonb_path_operator_for_postgres() -> None:
    assert json_text_expr(_Db(), "metadata", "skillport", "category") == (
        "metadata #>> '{skillport,category}'"
    )


def test_json_array_contains_condition_uses_jsonb_contains_for_postgres() -> None:
    condition, params = json_array_contains_condition(_Db(), "tags", "gobby")

    assert condition == "tags @> %s::jsonb"
    assert params == ('["gobby"]',)


def test_task_list_label_filter_uses_postgres_jsonb_contains() -> None:
    db = _CaptureDb()
    manager = LocalTaskManager(db)  # type: ignore[arg-type]

    assert (
        manager.list_tasks(project_id="proj1", label="interactive:planning-in-progress:sess") == []
    )

    task_query_index = next(
        index for index, query in enumerate(db.queries) if query.startswith("SELECT * FROM tasks")
    )
    task_query = db.queries[task_query_index]

    assert "tasks.labels @> %s::jsonb" in task_query
    assert db.params[task_query_index] == (
        "proj1",
        '["interactive:planning-in-progress:sess"]',
        50,
        0,
    )


def test_skill_list_negative_limit_omits_limit_for_postgres() -> None:
    db = _CaptureDb()
    manager = LocalSkillManager(db)  # type: ignore[arg-type]

    assert manager.list_skills(project_id=None, include_global=False, limit=-1) == []

    query = db.queries[-1]
    assert "LIMIT" not in query
    assert "OFFSET" not in query
    assert db.params[-1] == ()


def test_timestamp_helpers_emit_postgres_native_time_arithmetic() -> None:
    db = _Db()

    assert older_than_now_expr(db, "updated_at", "%s", "hour") == (
        "updated_at < NOW() - (%s::double precision * INTERVAL '1 hour')"
    )
    assert newer_than_now_expr(db, "created_at", "%s", "minute") == (
        "created_at >= NOW() - (%s::double precision * INTERVAL '1 minute')"
    )
    assert timestamp_plus_seconds_before_now_expr(db, "started_at", "timeout_seconds") == (
        "started_at + (timeout_seconds * INTERVAL '1 second') < NOW()"
    )
    assert elapsed_seconds_greater_than_expr(db, "last_activity_at", "timeout_seconds") == (
        "EXTRACT(EPOCH FROM (NOW() - last_activity_at)) > timeout_seconds"
    )


def test_table_column_names_uses_information_schema_for_postgres() -> None:
    db = _CaptureDb()

    def fetchall(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, str]]:
        db.queries.append(query)
        db.params.append(params)
        return [{"name": "id"}, {"name": "title"}]

    db.fetchall = fetchall  # type: ignore[method-assign]

    assert table_column_names(db, "tasks") == {"id", "title"}
    assert "information_schema.columns" in db.queries[-1]
    assert db.params[-1] == ("tasks",)


def test_table_column_names_rejects_invalid_table_name() -> None:
    db = _CaptureDb()

    with pytest.raises(ValueError, match="Invalid table name"):
        table_column_names(db, "tasks); DROP TABLE tasks; --")


def test_completion_notification_query_uses_postgres_jsonb_without_json_valid() -> None:
    db = _CaptureDb()
    manager = InterSessionMessageManager(db)  # type: ignore[arg-type]

    assert manager.has_completion_notification("sess", "completion", "run-1") is False

    query = db.queries[-1]
    assert "metadata_json #>> '{completion_id}'" in query
    assert "metadata_json #>> '{run_id}'" in query
    assert "metadata_json #>> '{execution_id}'" in query
    assert "json_valid" not in query


def test_metric_snapshot_queries_use_postgres_intervals() -> None:
    db = _CaptureDb()
    storage = MetricSnapshotStorage(db)  # type: ignore[arg-type]

    assert storage.get_snapshots(hours=6) == []
    storage.delete_old_snapshots(retention_hours=24)

    assert "timestamp >= NOW() - (%s::double precision * INTERVAL '1 hour')" in db.queries[0]
    assert db.params[0] == (6, 120)
    assert "timestamp < NOW() - (%s::double precision * INTERVAL '1 hour')" in db.queries[1]
    assert db.params[1] == (24,)


def test_token_timeseries_uses_postgres_bucket_and_window_sql() -> None:
    db = _CaptureDb()
    store = TokenEventStore(db)  # type: ignore[arg-type]

    assert store.get_timeseries(hours=12, granularity="30m") == []

    query = db.queries[-1]
    assert "date_trunc('hour', event_at AT TIME ZONE 'UTC')" in query
    assert "EXTRACT(MINUTE FROM event_at AT TIME ZONE 'UTC')" in query
    assert "event_at >= NOW() - (%s::double precision * INTERVAL '1 hour')" in query
    assert db.params[-1] == (12,)
